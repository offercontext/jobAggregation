from __future__ import annotations

import copy
import hashlib
import json

import pytest

from offerpilot.ai.interview_preparation_proposals import (
    InterviewPreparationModelError,
    _repair_prompt,
    _system_prompt,
    generate_interview_preparation_proposal,
    safe_empty_interview_preparation_proposal,
    validate_interview_preparation,
)
from offerpilot.ai.types import Assistant


def test_provider_prompt_spells_out_evidence_reference_object_contract() -> None:
    prompt = _system_prompt()
    repair = _repair_prompt("invalid_item_shape")

    assert 'source","path","excerpt' in prompt
    assert "/jd/text" in prompt
    assert "/raw_text" in prompt
    assert "/knowledge_evidence/001" in prompt
    assert 'source","path","excerpt' in repair


def _snapshot() -> dict[str, object]:
    return {
        "event": {
            "id": 4,
            "application_id": 7,
            "event_type": "interview",
            "subtype": "technical",
            "round": 2,
            "scheduled_at": "2026-07-20T10:00:00Z",
            "duration_minutes": 45,
            "status": "todo",
        },
        "jd": {"text": "Build reliable APIs with Python and SQL."},
        "resume": {
            "id": 9,
            "content_json": {
                "experience": [{"highlights": ["Built reliable API services"]}],
                "skills": ["Python", "SQL"],
            },
        },
        "knowledge_evidence": [
            {
                "id": "evidence-1",
                "path": "/knowledge/evidence/evidence-1",
                "provider_path": "/knowledge_evidence/001",
                "excerpt": "A rollback is safe when the observable signal is defined first.",
            }
        ],
        "user_assertions": ["I led the migration personally."],
    }


def _v2_snapshot() -> dict[str, object]:
    snapshot = _snapshot()
    snapshot.update(
        {
            "input_contract": "interview-preparation-input-v2",
            "readiness_feedback_selection": {
                "present": True,
                "ordered_version_ids": [41],
            },
            "readiness_feedback_selection_fingerprint": "sha256:" + "a" * 64,
            "readiness_feedback": [
                {
                    "statement": "先澄清可靠性约束，再说明缓存一致性的取舍。",
                    "user_note": "这是上下文，不是支持证据。",
                    "source_event": {"round": 2, "subtype": "technical"},
                    "practice_state": "completed",
                    "evidence": [
                        {
                            "path": "/difficulty_points",
                            "excerpt": "cache consistency tradeoffs",
                            "excerpt_sha256": "sha256:"
                            + hashlib.sha256(
                                "cache consistency tradeoffs".encode("utf-8")
                            ).hexdigest(),
                        }
                    ],
                }
            ],
        }
    )
    return snapshot


def _ref(source: str, path: str, excerpt: str) -> dict[str, str]:
    return {"source": source, "path": path, "excerpt": excerpt}


def _proposal() -> dict[str, object]:
    return {
        "preparation_directions": [
            {
                "id": "direction-1",
                "text": "准备可靠 API 设计的取舍说明。",
                "evidence_refs": [
                    _ref("jd", "/jd/text", "Build reliable APIs with Python and SQL.")
                ],
            }
        ],
        "story_prompts": [
            {
                "id": "story-1",
                "text": "准备说明你构建 API 服务时的具体做法。",
                "evidence_refs": [
                    _ref("resume", "/experience/0/highlights/0", "Built reliable API services")
                ],
            }
        ],
        "review_points": [
            {
                "id": "review-1",
                "text": "复习如何先定义可观察的安全信号。",
                "evidence_refs": [
                    _ref(
                        "knowledge_evidence",
                        "/knowledge_evidence/001",
                        "A rollback is safe when the observable signal is defined first.",
                    )
                ],
            }
        ],
        "interviewer_questions": [
            {
                "id": "question-1",
                "text": "可以请面试官说明本岗位最关注的 API 可靠性场景吗？",
                "evidence_refs": [
                    _ref("jd", "/jd/text", "Build reliable APIs with Python and SQL.")
                ],
            }
        ],
        "items_to_clarify": [
            {
                "id": "clarify-1",
                "text": "需要确认岗位对 SQL 深度的具体期待。",
                "evidence_refs": [_ref("resume", "/skills/1", "SQL")],
            }
        ],
    }


class FakeModel:
    supports_json_schema = False

    def __init__(self, responses: list[object], error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls = 0
        self.messages: list[list[object]] = []
        self.response_formats: list[object] = []

    def complete(self, messages, tools, response_format=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.messages.append(messages)
        self.response_formats.append(response_format)
        if self.error is not None:
            raise self.error
        response = self.responses.pop(0)
        return Assistant(
            content=response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        )


def test_validator_accepts_five_evidence_gated_preparation_arrays() -> None:
    assert validate_interview_preparation(_proposal(), _snapshot()) == _proposal()


def test_v2_validator_accepts_only_frozen_readiness_provider_paths() -> None:
    from offerpilot.ai.interview_preparation_proposals import (
        validate_interview_preparation_v2,
    )

    payload = safe_empty_interview_preparation_proposal()
    payload["review_points"] = [
        {
            "id": "review-feedback-1",
            "text": "准备解释缓存一致性的取舍。",
            "evidence_refs": [
                _ref(
                    "confirmed_readiness_feedback",
                    "/readiness_feedback/0/evidence/0/excerpt",
                    "cache consistency tradeoffs",
                )
            ],
        }
    ]
    assert validate_interview_preparation_v2(payload, _v2_snapshot()) == payload

    forged = copy.deepcopy(payload)
    forged["review_points"][0]["evidence_refs"][0] = _ref(  # type: ignore[index]
        "confirmed_readiness_feedback",
        "/readiness_feedback/0/user_note",
        "这是上下文，不是支持证据。",
    )
    with pytest.raises(InterviewPreparationModelError) as exc_info:
        validate_interview_preparation_v2(forged, _v2_snapshot())
    assert exc_info.value.validation_category == "unknown_evidence_ref"


def test_v2_prompt_uses_bounded_untrusted_feedback_without_internal_ids() -> None:
    from offerpilot.ai.interview_preparation_proposals import (
        generate_interview_preparation_proposal_v2,
    )

    model = FakeModel([safe_empty_interview_preparation_proposal()])
    result = generate_interview_preparation_proposal_v2(model, _v2_snapshot())

    assert result == safe_empty_interview_preparation_proposal()
    system = model.messages[0][0].content
    prompt = model.messages[0][1].content
    assert "不受信任" in system
    assert "confirmed_readiness_feedback" in system
    assert "cache consistency tradeoffs" in prompt
    assert "这是上下文，不是支持证据。" in prompt
    assert "ordered_version_ids" not in prompt
    assert "selection_fingerprint" not in prompt


def test_v1_schema_prompt_and_validator_remain_closed_to_readiness_v2() -> None:
    from offerpilot.ai.interview_preparation_proposals import (
        INTERVIEW_PREPARATION_JSON_SCHEMA,
        _initial_prompt,
    )

    schema_sources = INTERVIEW_PREPARATION_JSON_SCHEMA["properties"][
        "review_points"
    ]["items"]["properties"]["evidence_refs"]["items"]["properties"][
        "source"
    ]["enum"]
    assert "confirmed_readiness_feedback" not in schema_sources
    assert "readiness_feedback" not in _initial_prompt(_v2_snapshot())

    payload = safe_empty_interview_preparation_proposal()
    payload["review_points"] = [
        {
            "id": "legacy-review-1",
            "text": "must stay rejected by V1",
            "evidence_refs": [
                _ref(
                    "confirmed_readiness_feedback",
                    "/readiness_feedback/0/statement",
                    "先澄清可靠性约束",
                )
            ],
        }
    ]
    with pytest.raises(InterviewPreparationModelError) as exc_info:
        validate_interview_preparation(payload, _v2_snapshot())
    assert exc_info.value.validation_category == "unknown_evidence_ref"


def test_readiness_feedback_final_wrapper_exact_utf8_budget() -> None:
    from offerpilot.review_readiness.preparation_selection import (
        PreparationReadinessSelectionError,
        canonical_readiness_feedback_bytes,
    )

    prefix = '中😀"\\'
    prefix_bytes = len(prefix.encode("utf-8"))
    feedback = [
        {
            "statement": "s" * 2048,
            "user_note": "u" * 1024,
            "source_event": {"round": index + 1, "subtype": "technical"},
            "practice_state": "completed",
            "evidence": [
                {
                    "path": "/difficulty_points",
                    "excerpt": prefix + "x" * (4096 - prefix_bytes),
                    "excerpt_sha256": "sha256:" + f"{index:x}" * 64,
                }
            ],
        }
        for index in range(8)
    ]
    raw = json.dumps(
        {"readiness_feedback": feedback},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    delta = 65_536 - len(raw)
    assert 0 < delta < 32_000
    for item in feedback:
        excerpt = item["evidence"][0]["excerpt"]  # type: ignore[index]
        available = excerpt.count("x")
        used = min(delta, available)
        item["evidence"][0]["excerpt"] = excerpt.replace("x", '"', used)  # type: ignore[index]
        delta -= used
        if delta == 0:
            break
    assert delta == 0
    assert len(canonical_readiness_feedback_bytes(feedback)) == 65_536
    for item in feedback:
        excerpt = item["evidence"][0]["excerpt"]  # type: ignore[index]
        item["evidence"][0]["excerpt_sha256"] = (  # type: ignore[index]
            "sha256:" + hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        )
    exact_snapshot = _snapshot()
    exact_snapshot.update(
        {
            "input_contract": "interview-preparation-input-v2",
            "readiness_feedback_selection": {
                "present": True,
                "ordered_version_ids": list(range(1, 9)),
            },
            "readiness_feedback_selection_fingerprint": "sha256:" + "a" * 64,
            "readiness_feedback": feedback,
        }
    )
    from offerpilot.ai.interview_preparation_proposals import (
        validate_interview_preparation_v2,
    )

    assert validate_interview_preparation_v2(
        safe_empty_interview_preparation_proposal(), exact_snapshot
    ) == safe_empty_interview_preparation_proposal()

    oversized = copy.deepcopy(feedback)
    for item in oversized:
        excerpt = item["evidence"][0]["excerpt"]  # type: ignore[index]
        if "x" in excerpt:
            item["evidence"][0]["excerpt"] = excerpt.replace("x", "\\", 1)  # type: ignore[index]
            changed = item["evidence"][0]["excerpt"]  # type: ignore[index]
            item["evidence"][0]["excerpt_sha256"] = (  # type: ignore[index]
                "sha256:" + hashlib.sha256(changed.encode("utf-8")).hexdigest()
            )
            break
    with pytest.raises(PreparationReadinessSelectionError) as exc_info:
        canonical_readiness_feedback_bytes(oversized)
    assert exc_info.value.code == "preparation_readiness_feedback_too_large"
    oversized_snapshot = {**exact_snapshot, "readiness_feedback": oversized}
    with pytest.raises(InterviewPreparationModelError) as model_exc:
        validate_interview_preparation_v2(
            safe_empty_interview_preparation_proposal(), oversized_snapshot
        )
    assert model_exc.value.validation_category == "limit_exceeded"


def test_validator_rejects_forged_refs_non_leaf_resume_and_unicode_rewrite() -> None:
    snapshot = _snapshot()
    payload = copy.deepcopy(_proposal())
    payload["story_prompts"][0]["evidence_refs"][0] = _ref(  # type: ignore[index]
        "resume", "/experience/0", "Built reliable API services"
    )
    with pytest.raises(InterviewPreparationModelError) as exc_info:
        validate_interview_preparation(payload, snapshot)
    assert exc_info.value.validation_category == "unknown_evidence_ref"

    payload = copy.deepcopy(_proposal())
    payload["review_points"][0]["evidence_refs"][0]["excerpt"] = (  # type: ignore[index]
        "A rollback is safe when the observable signal is defined first.\u00a0"
    )
    with pytest.raises(InterviewPreparationModelError) as exc_info:
        validate_interview_preparation(payload, snapshot)
    assert exc_info.value.validation_category == "excerpt_mismatch"


@pytest.mark.parametrize(
    ("source", "path", "excerpt"),
    [
        ("jd", "/jd/text", " \t"),
        ("resume", "/skills/0", "\n"),
        ("knowledge_evidence", "/knowledge_evidence/001", "  "),
    ],
)
def test_validator_rejects_blank_evidence_excerpts(
    source: str, path: str, excerpt: str
) -> None:
    payload = copy.deepcopy(_proposal())
    payload["preparation_directions"][0]["evidence_refs"][0] = _ref(  # type: ignore[index]
        source, path, excerpt
    )
    with pytest.raises(InterviewPreparationModelError) as exc_info:
        validate_interview_preparation(payload, _snapshot())
    assert exc_info.value.validation_category == "excerpt_mismatch"


def test_validator_rejects_duplicate_ids_across_arrays_and_noncanonical_array_pointer() -> None:
    payload = copy.deepcopy(_proposal())
    payload["story_prompts"][0]["id"] = payload["preparation_directions"][0]["id"]  # type: ignore[index]
    with pytest.raises(InterviewPreparationModelError) as exc_info:
        validate_interview_preparation(payload, _snapshot())
    assert exc_info.value.validation_category == "invalid_item_shape"

    payload = copy.deepcopy(_proposal())
    payload["story_prompts"][0]["evidence_refs"][0] = _ref(  # type: ignore[index]
        "resume", "/skills/00", "Python"
    )
    with pytest.raises(InterviewPreparationModelError) as exc_info:
        validate_interview_preparation(payload, _snapshot())
    assert exc_info.value.validation_category == "unknown_evidence_ref"


def test_validator_rejects_item_and_evidence_limits() -> None:
    payload = _proposal()
    payload["preparation_directions"] = [  # type: ignore[index]
        copy.deepcopy(payload["preparation_directions"][0]) for _ in range(9)  # type: ignore[index]
    ]
    with pytest.raises(InterviewPreparationModelError) as exc_info:
        validate_interview_preparation(payload, _snapshot())
    assert exc_info.value.validation_category == "limit_exceeded"

    payload = _proposal()
    payload["preparation_directions"][0]["evidence_refs"] = [  # type: ignore[index]
        _ref("jd", "/jd/text", "Build reliable APIs with Python and SQL.") for _ in range(6)
    ]
    with pytest.raises(InterviewPreparationModelError) as exc_info:
        validate_interview_preparation(payload, _snapshot())
    assert exc_info.value.validation_category == "limit_exceeded"


def test_generate_repairs_once_with_machine_failure_category() -> None:
    invalid = _proposal()
    invalid["preparation_directions"][0]["evidence_refs"] = []  # type: ignore[index]
    model = FakeModel([invalid, _proposal()])

    result = generate_interview_preparation_proposal(model, _snapshot())

    assert result == _proposal()
    assert model.calls == 2
    assert "missing_evidence_ref" in model.messages[1][-1].content
    assert "Built reliable API services" not in model.messages[1][-1].content


def test_provider_failure_is_called_once_and_not_repaired() -> None:
    model = FakeModel([], error=TimeoutError("private provider detail"))

    with pytest.raises(InterviewPreparationModelError) as exc_info:
        generate_interview_preparation_proposal(model, _snapshot())

    assert model.calls == 1
    assert exc_info.value.failure_category == "provider_error"


def test_two_invalid_outputs_return_validated_safe_empty_without_model_text() -> None:
    model = FakeModel(
        [
            {"preparation_directions": [{"text": "candidate secret"}]},
            {"unexpected": "raw model output"},
        ]
    )

    result = generate_interview_preparation_proposal(model, _snapshot())

    assert result == safe_empty_interview_preparation_proposal()
    assert model.calls == 2
    assert "candidate secret" not in json.dumps(result, ensure_ascii=False)


def test_diagnostic_distinguishes_direct_safe_empty_from_contract_failure() -> None:
    diagnostics: list[dict[str, object]] = []
    model = FakeModel([safe_empty_interview_preparation_proposal()])

    result = generate_interview_preparation_proposal(
        model, _snapshot(), on_diagnostic=diagnostics.append
    )

    assert result == safe_empty_interview_preparation_proposal()
    diagnostic = diagnostics[0]
    assert diagnostic["failure_category"] is None
    assert diagnostic["failure_categories"] == []
    assert diagnostic["repair_attempted"] is False
    assert diagnostic["retry_count"] == 0
    assert diagnostic["provider_request_id_hash"] == ""
    assert isinstance(diagnostic["duration_ms"], int)
    assert diagnostic["structure_summaries"][0]["payload_type"] == "object"


def test_diagnostic_records_repair_categories_and_provider_request_id_hash() -> None:
    class ProviderIdModel:
        supports_json_schema = False

        def __init__(self) -> None:
            self.responses = [{"unexpected": "raw model output"}, _proposal()]
            self.calls = 0

        def complete(self, messages, tools, response_format=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            return Assistant(
                content=json.dumps(self.responses.pop(0), ensure_ascii=False),
                provider_blocks={"request_id": "provider-request-id"},
            )

    diagnostics: list[dict[str, object]] = []
    result = generate_interview_preparation_proposal(
        ProviderIdModel(), _snapshot(), on_diagnostic=diagnostics.append
    )

    assert result == _proposal()
    assert diagnostics[0]["failure_category"] == "unexpected_field"
    assert diagnostics[0]["failure_categories"] == ["unexpected_field"]
    assert diagnostics[0]["repair_attempted"] is True
    assert diagnostics[0]["retry_count"] == 1
    assert diagnostics[0]["provider_request_id_hash"] == __import__("hashlib").sha256(
        b"provider-request-id"
    ).hexdigest()[:12]


def test_diagnostic_preserves_both_contract_failure_categories_without_raw_output() -> None:
    diagnostics: list[dict[str, object]] = []
    model = FakeModel(
        [
            {
                "preparation_directions": [{"text": "candidate secret"}],
                "story_prompts": [],
                "review_points": [],
                "interviewer_questions": [],
                "items_to_clarify": [],
            },
            {"unexpected": "raw model output"},
        ]
    )

    result = generate_interview_preparation_proposal(
        model, _snapshot(), on_diagnostic=diagnostics.append
    )

    encoded = json.dumps(diagnostics, ensure_ascii=False)
    assert result == safe_empty_interview_preparation_proposal()
    assert diagnostics[0]["failure_category"] == "unexpected_field"
    assert diagnostics[0]["failure_categories"] == [
        "invalid_item_shape",
        "unexpected_field",
    ]
    assert diagnostics[0]["repair_attempted"] is True
    assert diagnostics[0]["retry_count"] == 1
    assert "candidate secret" not in encoded
    assert "raw model output" not in encoded


def test_invalid_item_shape_diagnostic_keeps_only_redacted_structure_summary() -> None:
    diagnostics: list[dict[str, object]] = []
    invalid = {
        "preparation_directions": [{"text": "candidate secret"}],
        "story_prompts": [],
        "review_points": [],
        "interviewer_questions": [],
        "items_to_clarify": [],
    }

    result = generate_interview_preparation_proposal(
        FakeModel([invalid, _proposal()]), _snapshot(), on_diagnostic=diagnostics.append
    )

    assert result == _proposal()
    summaries = diagnostics[0]["structure_summaries"]
    assert isinstance(summaries, list)
    assert summaries[0]["payload_type"] == "object"
    assert summaries[0]["top_level_keys"] == [
        "interviewer_questions",
        "items_to_clarify",
        "preparation_directions",
        "review_points",
        "story_prompts",
    ]
    direction_shape = summaries[0]["fields"]["preparation_directions"]
    assert direction_shape["type"] == "array"
    assert direction_shape["length"] == 1
    assert direction_shape["item_key_sets"] == [["text"]]
    assert "candidate secret" not in json.dumps(diagnostics, ensure_ascii=False)


def test_invalid_item_shape_prompt_repeats_fixed_json_contract() -> None:
    invalid = {
        "preparation_directions": [{"text": "candidate secret"}],
        "story_prompts": [],
        "review_points": [],
        "interviewer_questions": [],
        "items_to_clarify": [],
    }
    model = FakeModel([invalid, _proposal()])

    generate_interview_preparation_proposal(model, _snapshot())

    initial_system = model.messages[0][0].content
    initial_user = model.messages[0][1].content
    repair_user = model.messages[1][1].content
    for prompt in (initial_system, initial_user, repair_user):
        assert "The top-level JSON object must have exactly these five keys" in prompt
        assert "preparation_directions, story_prompts, review_points, interviewer_questions, items_to_clarify" in prompt
        assert "Each array item must have exactly these keys: id, text, evidence_refs" in prompt
        assert "Each evidence_refs item must have exactly these keys: source, path, excerpt" in prompt
        assert '"source":"resume"' in prompt


def test_user_assertions_are_saved_in_snapshot_but_absent_from_provider_payload() -> None:
    model = FakeModel([_proposal()])

    generate_interview_preparation_proposal(model, _snapshot())

    provider_text = "\n".join(message.content for message in model.messages[0])
    assert "I led the migration personally." not in provider_text
    assert "Build reliable APIs with Python and SQL." in provider_text
    assert "A rollback is safe when the observable signal is defined first." in provider_text


def test_provider_payload_omits_internal_application_event_resume_and_note_ids() -> None:
    model = FakeModel([_proposal()])
    snapshot = _snapshot()
    generate_interview_preparation_proposal(model, snapshot)
    provider_text = "\n".join(message.content for message in model.messages[0])
    assert '"application_id"' not in provider_text
    assert '"event_id"' not in provider_text
    assert '"note_version_id"' not in provider_text
    assert "evidence-1" not in provider_text
    assert "/knowledge/evidence/evidence-1" not in provider_text
    assert '"id":4' not in provider_text
    assert '"id":9' not in provider_text


def test_json_schema_is_passed_only_for_explicit_true_capability() -> None:
    model = FakeModel([_proposal()])
    model.supports_json_schema = True

    generate_interview_preparation_proposal(model, _snapshot())

    assert model.response_formats[0]["type"] == "json_schema"  # type: ignore[index]

    unsupported = FakeModel([_proposal()])
    unsupported.supports_json_schema = "true"
    generate_interview_preparation_proposal(unsupported, _snapshot())
    assert unsupported.response_formats[0] is None
