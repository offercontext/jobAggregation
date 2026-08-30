from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from offerpilot.db import init_database
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
)
from offerpilot.repositories.interview_stories import (
    InterviewStoriesRepository,
    StoryConflictError,
    StoryValidationError,
    canonical_story_content,
    derive_story_source_states,
    materialize_selected_sources,
    story_request_fingerprint,
    validate_story_evidence_links,
)


def test_canonical_story_content_assigns_stable_target_ids_to_duplicate_text() -> None:
    content = canonical_story_content(
        {
            "title": "同一个项目",
            "blocks": [
                {"kind": "situation", "text": "重复文本", "fact_mode": "evidence_backed"},
                {"kind": "action", "text": "重复文本", "fact_mode": "evidence_backed"},
                {"kind": "reflection", "text": "我的复盘", "fact_mode": "user_view"},
            ],
            "capability_labels": ["沟通", "沟通"],
            "applicable_questions": ["你遇到过什么挑战？"],
            "fact_gap_codes": ["missing_result"],
        }
    )

    assert content["title"] == {"id": "title", "text": "同一个项目"}
    assert [block["id"] for block in content["blocks"]] == [
        "situation_001",
        "action_001",
        "reflection_001",
    ]
    assert [item["id"] for item in content["capability_labels"]] == [
        "capability_001",
        "capability_002",
    ]
    assert content["blocks"][2]["fact_mode"] == "user_view"


@pytest.mark.parametrize(
    "content",
    [
        {"title": "x" * 201, "blocks": [], "capability_labels": [], "applicable_questions": [], "fact_gap_codes": ["missing_result"]},
        {"title": "A story", "blocks": [{"kind": "situation", "text": "fact", "fact_mode": "evidence_backed"}], "capability_labels": [], "applicable_questions": [], "fact_gap_codes": []},
        {"title": "A story", "blocks": [{"kind": "result", "text": "result", "fact_mode": "evidence_backed"}], "capability_labels": [], "applicable_questions": [], "fact_gap_codes": ["missing_result"]},
    ],
)
def test_manual_story_content_uses_the_same_title_and_result_contract(content) -> None:
    with pytest.raises(StoryValidationError):
        canonical_story_content(content)


def test_materialize_selected_sources_limits_to_allowed_original_fields_and_paths(tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    with factory() as session:
        resume = Resume(
            name="筱哲",
            title="后端工程师",
            content_json=json.dumps({"项目/名": "处理 emoji 🚀 与 NFD e\u0301"}, ensure_ascii=False),
        )
        note = InterviewNote(
            company="星云数据",
            position="后端工程师",
            questions="如何排查线上延迟？",
            self_reflection="我先确认了指标。",
            difficulty_points="没有立即给出量化结果。",
            mood="平静",
        )
        attempt = MockInterviewAttempt(
            application_id=1,
            event_id=1,
            resume_id=1,
            idempotency_key="mock-story-source-key",
            input_snapshot_json="{}",
            source_fingerprint="mock-fingerprint",
            attempt_status="feedback_ready",
            transcript_fingerprint="transcript",
            completed_at=datetime.now(timezone.utc),
        )
        session.add_all([resume, note, attempt])
        session.flush()
        turn = MockInterviewTurn(
            attempt_id=attempt.id,
            turn_no=1,
            question_idempotency_key="mock-question-key",
            turn_idempotency_key="mock-answer-key",
            question_text="请介绍一个项目。",
            answer_text="我用分段定位解决了延迟。",
            turn_status="answered",
        )
        session.add(turn)
        session.commit()

        snapshot = materialize_selected_sources(
            session,
            [
                {"source_kind": "interview_note", "source_id": note.id, "path": "/questions"},
                {
                    "source_kind": "resume_version",
                    "source_id": resume.id,
                    "path": "/content_json/项目~1名",
                },
                {
                    "source_kind": "mock_turn",
                    "source_id": attempt.id,
                    "path": "/turns/001/answer",
                },
            ],
            ["我确认这是我负责的项目。"],
        )

    candidates = InterviewStoriesRepository(factory).list_source_candidates()
    factory.kw["bind"].dispose()

    assert [item["source_kind"] for item in snapshot.sources] == [
        "interview_note",
        "mock_turn",
        "resume_version",
        "user_assertion",
    ]
    assert snapshot.sources[2]["excerpt"] == "处理 emoji 🚀 与 NFD e\u0301"
    assert snapshot.sources[0]["excerpt"] == "如何排查线上延迟？"
    assert snapshot.sources[3]["path"] == "/statement"
    assert candidates["resumes"][0]["leaves"][0]["path"] == "/content_json/项目~1名"
    assert candidates["interview_notes"][0]["leaves"][0]["path"] == "/questions"
    assert candidates["mock_turns"][0]["leaves"][-1]["path"] == "/turns/001/answer"


def test_resume_pointer_rejects_extremely_long_ascii_array_index_without_value_error(tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    with factory() as session:
        resume = Resume(content_json=json.dumps(["candidate fact"]))
        session.add(resume)
        session.commit()
        with pytest.raises(StoryValidationError, match="path"):
            materialize_selected_sources(
                session,
                [{
                    "source_kind": "resume_version",
                    "source_id": resume.id,
                    "path": "/content_json/" + "1" * 5000,
                }],
                [],
            )
    factory.kw["bind"].dispose()


def test_user_assertion_can_support_an_explicit_story_statement_and_links_are_bounded(tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    with factory() as session:
        note = _create_note(session)
        session.commit()
        snapshot = materialize_selected_sources(
            session,
            [{"source_kind": "interview_note", "source_id": note.id, "path": "/questions"}],
            ["I would communicate risk earlier."],
        )
        content = canonical_story_content(_manual_content())
        links = _links_for_content(content, snapshot)
        star_link = next(link for link in links if link["target_id"] == "situation_001")
        assertion = next(item for item in snapshot.sources if item["source_kind"] == "user_assertion")
        star_link.update({
            "source_kind": assertion["source_kind"],
            "source_stable_id": assertion["source_stable_id"],
            "source_version_or_snapshot": assertion["source_version_or_snapshot"],
            "source_path": assertion["path"],
            "excerpt": assertion["excerpt"],
        })
        canonical = validate_story_evidence_links(content, links, snapshot)
        assert next(link for link in canonical if link.target_id == "situation_001").source_kind == "user_assertion"

        links = _links_for_content(content, snapshot)
        title = next(link for link in links if link["target_kind"] == "title")
        links.extend([dict(title) for _ in range(8)])
        with pytest.raises(StoryValidationError, match="count exceeds limit"):
            validate_story_evidence_links(content, links, snapshot)
    factory.kw["bind"].dispose()


def test_provider_unknown_replay_with_live_lease_never_calls_provider_again(tmp_path) -> None:
    factory = init_database(tmp_path / "story-provider-unknown-live-lease.db")
    repository = InterviewStoriesRepository(factory)
    clock = [datetime(2026, 8, 10, tzinfo=timezone.utc)]
    with factory() as session:
        note = _create_note(session)
        note_id = note.id
        session.commit()

    request = {
        "target_story_id": None,
        "expected_current_version_id": None,
        "expected_story_revision": None,
        "selections": [{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        "assertions": ["I owned this incident response."],
        "idempotency_key": "story-live-provider-unknown-key",
        "entrypoint": "ui",
        "now_factory": lambda: clock[0],
    }
    first = repository.claim_proposal(**request)
    assert repository.mark_provider_unknown(
        attempt_id=first.attempt_id,
        generation_revision=first.generation_revision,
        provider_call_token=first.provider_call_token,
        category="provider_error",
    )
    assert repository.get_attempt_retry_after_ms(
        first.attempt_id, now_factory=lambda: clock[0]
    ) == 30_250

    replay = repository.claim_proposal(**request)

    assert replay.pending is True
    assert replay.should_call_provider is False
    assert replay.generation_revision == first.generation_revision
    with factory() as session:
        attempt = session.get(InterviewStoryProposalAttempt, first.attempt_id)
        assert attempt is not None
        assert attempt.attempt_status == "provider_unknown"
        assert attempt.provider_lease_until is not None
    factory.kw["bind"].dispose()


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        ({"source_kind": "knowledge", "source_id": 1, "path": "/text"}, "source kind"),
        ({"source_kind": "interview_note", "source_id": 1, "path": "/proposal_json"}, "path"),
        ({"source_kind": "mock_turn", "source_id": 1, "path": "/turns/1/answer"}, "path"),
        ({"source_kind": "mock_turn", "source_id": 1, "path": "/turns/\u0660\u0660\u0661/answer"}, "path"),
        ({"source_kind": "resume_version", "source_id": 1, "path": "/title"}, "path"),
        ({"source_kind": "resume_version", "source_id": 1, "path": "/content_json/a~2b"}, "path"),
    ],
)
def test_materialize_selected_sources_rejects_non_story_evidence(selection, message, tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    with factory() as session:
        with pytest.raises(StoryValidationError, match=message):
            materialize_selected_sources(session, [selection], [])
    factory.kw["bind"].dispose()


def test_resume_source_rejects_unicode_digit_array_index_alias(tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    with factory() as session:
        resume = Resume(
            name="筱哲",
            content_json=json.dumps({"items": ["可引用文本"]}, ensure_ascii=False),
        )
        session.add(resume)
        session.commit()

        with pytest.raises(StoryValidationError, match="path"):
            materialize_selected_sources(
                session,
                [
                    {
                        "source_kind": "resume_version",
                        "source_id": resume.id,
                        "path": "/content_json/items/١",
                    }
                ],
                [],
            )
    factory.kw["bind"].dispose()


def test_materialize_selected_sources_rejects_duplicate_source_selection(tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    with factory() as session:
        note = _create_note(session)
        session.commit()

        with pytest.raises(StoryValidationError, match="selection is duplicated"):
            materialize_selected_sources(
                session,
                [
                    {"source_kind": "interview_note", "source_id": note.id, "path": "/questions"},
                    {"source_kind": "interview_note", "source_id": note.id, "path": "/questions"},
                ],
                [],
            )
    factory.kw["bind"].dispose()


def _manual_content() -> dict[str, object]:
    return {
        "title": "Order service recovery",
        "blocks": [
            {"kind": "situation", "text": "Latency increased", "fact_mode": "evidence_backed"},
            {"kind": "action", "text": "I isolated the bottleneck", "fact_mode": "evidence_backed"},
            {"kind": "reflection", "text": "I should communicate earlier", "fact_mode": "user_view"},
        ],
        "capability_labels": ["incident response"],
        "applicable_questions": ["Tell me about an incident"],
        "fact_gap_codes": ["missing_result"],
    }


def _links_for_content(content, snapshot) -> list[dict[str, str]]:
    note = next(item for item in snapshot.sources if item["source_kind"] == "interview_note")
    assertion = next(item for item in snapshot.sources if item["source_kind"] == "user_assertion")

    def source_fields(item: dict[str, str]) -> dict[str, str]:
        return {
            "source_kind": item["source_kind"],
            "source_stable_id": item["source_stable_id"],
            "source_version_or_snapshot": item["source_version_or_snapshot"],
            "source_path": item["path"],
            "excerpt": item["excerpt"],
        }

    targets = [
        ("title", content["title"]["id"]),
        *[("block", item["id"]) for item in content["blocks"]],
        *[("capability_label", item["id"]) for item in content["capability_labels"]],
        *[("applicable_question", item["id"]) for item in content["applicable_questions"]],
    ]
    return [
        {
            "target_kind": target_kind,
            "target_id": target_id,
            **source_fields(assertion if target_id == "reflection_001" else note),
        }
        for target_kind, target_id in targets
    ]


def _create_note(session) -> InterviewNote:
    note = InterviewNote(
        company="Nebula Data",
        position="Backend Engineer",
        questions="How did you handle the incident?",
        self_reflection="I should communicate earlier",
        difficulty_points="I need a measurable result",
        mood="calm",
    )
    session.add(note)
    session.flush()
    return note


def test_evidence_links_require_exact_target_and_frozen_source_identity(tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    with factory() as session:
        note = _create_note(session)
        session.commit()
        content = canonical_story_content(_manual_content())
        snapshot = materialize_selected_sources(
            session,
            [{"source_kind": "interview_note", "source_id": note.id, "path": "/questions"}],
            ["I own this incident response."],
        )
        links = _links_for_content(content, snapshot)

        canonical = validate_story_evidence_links(content, links, snapshot)
        assert len(canonical) == 6

        forged = [dict(item) for item in links]
        forged[0]["source_path"] = "/self_reflection"
        with pytest.raises(StoryValidationError, match="source"):
            validate_story_evidence_links(content, forged, snapshot)

        incomplete = links[:-1]
        with pytest.raises(StoryValidationError, match="targets require evidence"):
            validate_story_evidence_links(content, incomplete, snapshot)
    factory.kw["bind"].dispose()


def test_evidence_links_reject_mixed_client_and_frozen_source_identity_shapes(tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    with factory() as session:
        note = _create_note(session)
        session.commit()
        content = canonical_story_content(_manual_content())
        snapshot = materialize_selected_sources(
            session,
            [{"source_kind": "interview_note", "source_id": note.id, "path": "/questions"}],
            ["I own this incident response."],
        )
        links = _links_for_content(content, snapshot)
        mixed = [dict(item) for item in links]
        mixed[0]["source_id"] = note.id

        with pytest.raises(StoryValidationError, match="extra fields"):
            validate_story_evidence_links(content, mixed, snapshot)
    factory.kw["bind"].dispose()


def test_manual_story_version_cas_assertions_and_read_time_source_states(tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    repository = InterviewStoriesRepository(factory)
    with factory() as session:
        note = _create_note(session)
        note_id = note.id
        session.commit()
        content = canonical_story_content(_manual_content())
        snapshot = materialize_selected_sources(
            session,
            [{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
            ["I own this incident response."],
        )
    created = repository.create_manual_story(
        content=_manual_content(),
        evidence_links=_links_for_content(content, snapshot),
        selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        assertions=["I own this incident response."],
        expected_current_version_id=None,
        idempotency_key="manual-story-version-cas-0001",
    )
    version_id = created["current_version_id"]
    assert created["story_revision"] == 1
    assert created["version"]["version_number"] == 1
    assert created["version"]["assertions"] == [
        {"id": created["version"]["assertions"][0]["id"], "statement": "I own this incident response.", "frozen": True}
    ]
    assertion_link = next(
        item for item in created["version"]["evidence_links"] if item["source_kind"] == "user_assertion"
    )
    assert assertion_link["source_stable_id"] == str(created["version"]["assertions"][0]["id"])

    with pytest.raises(StoryConflictError, match="stale"):
        repository.create_manual_version(
            story_id=created["id"],
            content=_manual_content(),
            evidence_links=_links_for_content(content, snapshot),
            selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
            assertions=["I own this incident response."],
            expected_current_version_id=version_id,
            expected_story_revision=99,
            idempotency_key="manual-story-version-cas-0002",
        )

    with factory() as session:
        assert session.scalar(select(InterviewStoryVersion).where(InterviewStoryVersion.story_id == created["id"])) is not None
        assert len(list(session.scalars(select(InterviewStoryUserAssertion)))) == 1
        note = session.get(InterviewNote, note_id)
        assert note is not None
        note.questions = "How did you coordinate the recovery?"
        session.commit()
        version = session.get(InterviewStoryVersion, version_id)
        assert version is not None
        states = derive_story_source_states(session, version)
        assert {item["state"] for item in states} >= {"changed", "frozen_user_assertion"}
        assert all("source_version_or_snapshot" in item for item in states)
        assert session.scalar(select(InterviewStoryVersionEvidenceLink).where(InterviewStoryVersionEvidenceLink.story_version_id == version_id)) is not None
        assert session.get(InterviewStory, created["id"]).current_version_id == version_id

    archived = repository.archive(story_id=created["id"], expected_story_revision=1)
    assert archived["status"] == "archived"
    with pytest.raises(StoryConflictError, match="archived"):
        repository.create_manual_version(
            story_id=created["id"],
            content=_manual_content(),
            evidence_links=_links_for_content(content, snapshot),
            selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
            assertions=["I own this incident response."],
            expected_current_version_id=version_id,
            expected_story_revision=2,
            idempotency_key="manual-story-version-cas-0003",
        )
    restored = repository.restore(story_id=created["id"], expected_story_revision=2)
    assert restored["status"] == "active"
    factory.kw["bind"].dispose()


def test_invalid_manual_save_rolls_back_all_story_rows_and_fingerprint_is_order_stable(tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    repository = InterviewStoriesRepository(factory)
    with factory() as session:
        note = _create_note(session)
        session.commit()
        with pytest.raises(StoryValidationError, match="targets require evidence"):
            repository.create_manual_story(
                content=_manual_content(),
                evidence_links=[],
                selections=[{"source_kind": "interview_note", "source_id": note.id, "path": "/questions"}],
                assertions=["I own this incident response."],
                expected_current_version_id=None,
                idempotency_key="manual-story-invalid-save-0001",
            )
        assert list(session.scalars(select(InterviewStory))) == []
        assert list(session.scalars(select(InterviewStoryVersion))) == []
        assert list(session.scalars(select(InterviewStoryUserAssertion))) == []

    selections = [
        {"source_kind": "interview_note", "source_id": 2, "path": "/questions"},
        {"source_kind": "resume_version", "source_id": 1, "path": "/content_json/name"},
    ]
    assert story_request_fingerprint(
        target_story_id=None,
        expected_current_version_id=None,
        expected_story_revision=None,
        selections=selections,
        assertions=["statement"],
    ) == story_request_fingerprint(
        target_story_id=None,
        expected_current_version_id=None,
        expected_story_revision=None,
        selections=list(reversed(selections)),
        assertions=["statement"],
    )
    factory.kw["bind"].dispose()


def test_manual_save_replays_the_same_key_without_creating_a_second_version(tmp_path) -> None:
    factory = init_database(tmp_path / "story.db")
    repository = InterviewStoriesRepository(factory)
    with factory() as session:
        note = _create_note(session)
        note_id = note.id
        session.commit()
        content = canonical_story_content(_manual_content())
        snapshot = materialize_selected_sources(
            session,
            [{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
            ["I own this incident response."],
        )
    request = {
        "content": _manual_content(),
        "evidence_links": _links_for_content(content, snapshot),
        "selections": [{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        "assertions": ["I own this incident response."],
        "expected_current_version_id": None,
        "idempotency_key": "manual-story-replay-key-0001",
    }

    first = repository.create_manual_story(**request)
    version_request = {
        **request,
        "expected_current_version_id": first["current_version_id"],
        "expected_story_revision": first["story_revision"],
        "idempotency_key": "manual-story-replay-key-0002",
    }
    repository.create_manual_version(story_id=first["id"], **version_request)
    replay = repository.create_manual_story(**request)

    assert replay["id"] == first["id"]
    assert replay["version"]["id"] == first["current_version_id"]
    with factory() as session:
        assert len(list(session.scalars(select(InterviewStory)))) == 1
        assert len(list(session.scalars(select(InterviewStoryVersion)))) == 2
        attempt = session.scalar(select(InterviewStoryProposalAttempt).where(InterviewStoryProposalAttempt.idempotency_key == request["idempotency_key"]))
        assert attempt is not None and attempt.attempt_status == "confirmed"
    factory.kw["bind"].dispose()


def _provider_story_proposal(snapshot) -> dict[str, object]:
    note = next(item for item in snapshot.sources if item["source_kind"] == "interview_note")
    assertion = next(item for item in snapshot.sources if item["source_kind"] == "user_assertion")

    def ref(item):
        return {
            "source_kind": item["source_kind"],
            "source_stable_id": item["source_stable_id"],
            "source_version_or_snapshot": item["source_version_or_snapshot"],
            "source_path": item["path"],
            "excerpt": item["excerpt"],
        }

    return {
        "title": {"text": "Incident recovery", "evidence_refs": [ref(note)]},
        "blocks": [
            {
                "kind": "situation",
                "text": "Latency investigation",
                "fact_mode": "evidence_backed",
                "evidence_refs": [ref(note)],
            },
            {
                "kind": "reflection",
                "text": "Communicate earlier",
                "fact_mode": "user_view",
                "evidence_refs": [ref(assertion)],
            },
        ],
        "capability_labels": [{"text": "incident response", "evidence_refs": [ref(note)]}],
        "applicable_questions": [{"text": "Describe incident response", "evidence_refs": [ref(note)]}],
        "fact_gap_codes": ["missing_result"],
    }


def _manual_content_from_proposal(proposal: dict[str, object]) -> dict[str, object]:
    return {
        "title": proposal["content"]["title"]["text"],
        "blocks": [
            {key: block[key] for key in ("kind", "text", "fact_mode")}
            for block in proposal["content"]["blocks"]
        ],
        "capability_labels": [item["text"] for item in proposal["content"]["capability_labels"]],
        "applicable_questions": [item["text"] for item in proposal["content"]["applicable_questions"]],
        "fact_gap_codes": proposal["content"]["fact_gap_codes"],
    }


def test_provider_result_source_deletion_invalidates_the_claim(tmp_path) -> None:
    from offerpilot.ai.interview_stories import safe_empty_interview_story_proposal
    from offerpilot.repositories.interview_stories import StorySourceConflictError

    factory = init_database(tmp_path / "story-source-conflict.db")
    repository = InterviewStoriesRepository(factory)
    with factory() as session:
        note = _create_note(session)
        note_id = note.id
        session.commit()
    claim = repository.claim_proposal(
        target_story_id=None,
        expected_current_version_id=None,
        expected_story_revision=None,
        selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        assertions=[],
        idempotency_key="story-source-deletion-key-01",
        entrypoint="ui",
    )
    with factory() as session:
        session.delete(session.get(InterviewNote, note_id))
        session.commit()
    with pytest.raises(StorySourceConflictError, match="source changed"):
        repository.complete_proposal(
            attempt_id=claim.attempt_id,
            generation_revision=claim.generation_revision,
            provider_call_token=claim.provider_call_token,
            proposal=safe_empty_interview_story_proposal(),
        )
    assert repository.get_attempt(claim.attempt_id)["attempt_status"] == "invalidated"
    with factory() as session:
        attempt = session.get(InterviewStoryProposalAttempt, claim.attempt_id)
        assert attempt is not None and attempt.provider_lease_until is None
    factory.kw["bind"].dispose()


def test_target_story_change_during_provider_immediately_invalidates_the_attempt_and_clears_lease(tmp_path) -> None:
    from offerpilot.ai.interview_stories import safe_empty_interview_story_proposal

    factory = init_database(tmp_path / "story-target-conflict.db")
    repository = InterviewStoriesRepository(factory)
    with factory() as session:
        note = _create_note(session)
        note_id = note.id
        session.commit()
        content = canonical_story_content(_manual_content())
        snapshot = materialize_selected_sources(
            session,
            [{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
            ["I owned this incident response."],
        )
    story = repository.create_manual_story(
        content=_manual_content(),
        evidence_links=_links_for_content(content, snapshot),
        selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        assertions=["I owned this incident response."],
        expected_current_version_id=None,
        idempotency_key="story-target-conflict-create-01",
    )
    claim = repository.claim_proposal(
        target_story_id=story["id"],
        expected_current_version_id=story["current_version_id"],
        expected_story_revision=story["story_revision"],
        selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        assertions=["I owned this incident response."],
        idempotency_key="story-target-conflict-claim-01",
        entrypoint="ui",
    )
    repository.create_manual_version(
        story_id=story["id"],
        content=_manual_content(),
        evidence_links=_links_for_content(content, snapshot),
        selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        assertions=["I owned this incident response."],
        expected_current_version_id=story["current_version_id"],
        expected_story_revision=story["story_revision"],
        idempotency_key="story-target-conflict-revision-01",
    )

    with factory() as session:
        attempt = session.get(InterviewStoryProposalAttempt, claim.attempt_id)
        assert attempt is not None
        assert attempt.attempt_status == "invalidated"
        assert attempt.failure_category == "source_changed"
        assert attempt.provider_lease_until is None
    assert not repository.complete_proposal(
        attempt_id=claim.attempt_id,
        generation_revision=claim.generation_revision,
        provider_call_token=claim.provider_call_token,
        proposal=safe_empty_interview_story_proposal(),
    )
    factory.kw["bind"].dispose()


def test_ready_attempt_detects_when_frozen_source_changes(tmp_path) -> None:
    from offerpilot.ai.interview_stories import validate_interview_story_proposal

    factory = init_database(tmp_path / "story-confirm-source-conflict.db")
    repository = InterviewStoriesRepository(factory)
    with factory() as session:
        note = _create_note(session)
        note_id = note.id
        session.commit()
    claim = repository.claim_proposal(
        target_story_id=None,
        expected_current_version_id=None,
        expected_story_revision=None,
        selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        assertions=["I own this incident response."],
        idempotency_key="story-confirm-source-change-01",
        entrypoint="ui",
    )
    provider_payload = _provider_story_proposal(claim.source_snapshot)
    checked = validate_interview_story_proposal(provider_payload, claim.source_snapshot)
    assert repository.complete_proposal(
        attempt_id=claim.attempt_id,
        generation_revision=claim.generation_revision,
        provider_call_token=claim.provider_call_token,
        proposal=provider_payload,
    )
    with factory() as session:
        note = session.get(InterviewNote, note_id)
        assert note is not None
        note.questions = "The source has changed after proposal generation."
        session.commit()
    assert checked["proposal_status"] == "normal"
    with factory() as session:
        current = materialize_selected_sources(
            session,
            [{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
            ["I own this incident response."],
        )
    assert current.source_fingerprint != claim.source_fingerprint
    assert repository.get_attempt(claim.attempt_id)["attempt_status"] == "ready"
    assert not hasattr(repository, "confirm_attempt")
    factory.kw["bind"].dispose()


def test_story_assertions_and_evidence_links_reject_duplicates_before_persistence(tmp_path) -> None:
    factory = init_database(tmp_path / "story-duplicate-input.db")
    with factory() as session:
        note = _create_note(session)
        session.commit()
        with pytest.raises(StoryValidationError, match="assertion is duplicated"):
            materialize_selected_sources(
                session,
                [{"source_kind": "interview_note", "source_id": note.id, "path": "/questions"}],
                ["I own this incident response.", "I own this incident response."],
            )
        content = canonical_story_content(_manual_content())
        snapshot = materialize_selected_sources(
            session,
            [{"source_kind": "interview_note", "source_id": note.id, "path": "/questions"}],
            ["I own this incident response."],
        )
        links = _links_for_content(content, snapshot)
        with pytest.raises(StoryValidationError, match="evidence link is duplicated"):
            validate_story_evidence_links(content, [*links, dict(links[0])], snapshot)
    factory.kw["bind"].dispose()


def test_story_attempt_replay_heartbeat_and_ready_publication_are_fenced(tmp_path) -> None:
    from offerpilot.ai.interview_stories import validate_interview_story_proposal

    factory = init_database(tmp_path / "story.db")
    repository = InterviewStoriesRepository(factory)
    clock = [datetime(2026, 8, 10, tzinfo=timezone.utc)]
    with factory() as session:
        note = _create_note(session)
        note_id = note.id
        session.commit()

    request = {
        "target_story_id": None,
        "expected_current_version_id": None,
        "expected_story_revision": None,
        "selections": [{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        "assertions": ["I own this incident response."],
        "idempotency_key": "story-attempt-key-0001",
        "entrypoint": "ui",
        "now_factory": lambda: clock[0],
    }
    first = repository.claim_proposal(**request)
    assert first.should_call_provider is True
    second = repository.claim_proposal(**request)
    assert second.pending is True
    assert second.should_call_provider is False

    heartbeat = repository.start_heartbeat(
        attempt_id=first.attempt_id,
        generation_revision=first.generation_revision,
        provider_call_token=first.provider_call_token,
        now_factory=lambda: clock[0],
    )
    heartbeat.stop()
    assert heartbeat.alive is False
    clock[0] += timedelta(seconds=31)
    assert heartbeat.tick() is True
    replay_after_old_lease = repository.claim_proposal(**request)
    assert replay_after_old_lease.pending is True
    assert replay_after_old_lease.generation_revision == first.generation_revision

    provider_payload = _provider_story_proposal(first.source_snapshot)
    assert validate_interview_story_proposal(
        provider_payload, first.source_snapshot
    )["proposal_status"] == "normal"
    assert repository.complete_proposal(
        attempt_id=first.attempt_id,
        generation_revision=first.generation_revision,
        provider_call_token=first.provider_call_token,
        proposal=provider_payload,
    )
    finished = repository.get_attempt(first.attempt_id)
    assert finished is not None and finished["attempt_status"] == "ready"
    with factory() as session:
        attempt = session.get(InterviewStoryProposalAttempt, first.attempt_id)
        assert attempt is not None
        attempt.provider_lease_until = datetime(2030, 1, 1)
        session.commit()

    with factory() as session:
        assert len(list(session.scalars(select(InterviewStoryVersion)))) == 0
        attempt = session.get(InterviewStoryProposalAttempt, first.attempt_id)
        assert attempt is not None and attempt.provider_lease_until == datetime(2030, 1, 1)
        assert attempt.attempt_status == "ready"
    factory.kw["bind"].dispose()


def test_proposal_confirmation_must_use_the_cas_values_frozen_at_claim(tmp_path) -> None:
    from offerpilot.ai.interview_stories import validate_interview_story_proposal

    factory = init_database(tmp_path / "story.db")
    repository = InterviewStoriesRepository(factory)
    with factory() as session:
        note = _create_note(session)
        note_id = note.id
        session.commit()
        initial_content = canonical_story_content(_manual_content())
        initial_snapshot = materialize_selected_sources(
            session,
            [{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
            ["I own this incident response."],
        )
    story = repository.create_manual_story(
        content=_manual_content(),
        evidence_links=_links_for_content(initial_content, initial_snapshot),
        selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        assertions=["I own this incident response."],
        expected_current_version_id=None,
        idempotency_key="manual-story-frozen-cas-0001",
    )
    claim = repository.claim_proposal(
        target_story_id=story["id"],
        expected_current_version_id=story["current_version_id"],
        expected_story_revision=story["story_revision"],
        selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        assertions=["I own this incident response."],
        idempotency_key="story-frozen-confirm-cas-0001",
        entrypoint="ui",
    )
    checked = validate_interview_story_proposal(
        _provider_story_proposal(claim.source_snapshot), claim.source_snapshot
    )
    assert repository.complete_proposal(
        attempt_id=claim.attempt_id,
        generation_revision=claim.generation_revision,
        provider_call_token=claim.provider_call_token,
        proposal=_provider_story_proposal(claim.source_snapshot),
    )
    advanced = repository.create_manual_version(
        story_id=story["id"],
        content=_manual_content(),
        evidence_links=_links_for_content(initial_content, initial_snapshot),
        selections=[{"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}],
        assertions=["I own this incident response."],
        expected_current_version_id=story["current_version_id"],
        expected_story_revision=story["story_revision"],
        idempotency_key="manual-story-frozen-cas-0002",
    )
    confirmation_links = [
        {
            key: value
            for key, value in link.items()
            if key in {
                "target_kind", "target_id", "source_kind", "source_stable_id",
                "source_version_or_snapshot", "source_path", "excerpt", "text_location",
            }
        }
        for link in checked["evidence_links"]
    ]
    with pytest.raises(StoryConflictError, match="confirmation CAS"):
        repository.prepare_confirmation_decision(
            attempt_id=claim.attempt_id,
            confirmation_token="story-frozen-confirm-cas-token",
            content=_manual_content_from_proposal(checked),
            evidence_links=confirmation_links,
            expected_current_version_id=advanced["current_version_id"],
            expected_story_revision=advanced["story_revision"],
        )
    with factory() as session:
        assert session.get(InterviewStoryProposalAttempt, claim.attempt_id).attempt_status == "ready"
        assert len(list(session.scalars(select(InterviewStoryVersion)))) == 2
    factory.kw["bind"].dispose()
