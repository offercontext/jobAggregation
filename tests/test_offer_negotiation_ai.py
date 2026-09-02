from __future__ import annotations

import json

import pytest

from offerpilot.ai.offer_negotiation import (
    OFFER_NEGOTIATION_FIELDS,
    OfferNegotiationModelError,
    build_offer_negotiation_snapshot,
    generate_offer_negotiation_proposal,
    safe_empty_offer_negotiation_proposal,
    validate_offer_negotiation,
)
from offerpilot.ai.offer_negotiation_templates import (
    TEMPLATE_IDS,
    build_template_catalog,
)
from offerpilot.ai.types import Assistant


class FakeModel:
    supports_json_schema = False

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list] = []

    def complete(self, messages, tools, response_format=None):
        self.calls.append([messages, response_format])
        return Assistant(content=self.responses.pop(0), provider_blocks={"request_id": "req-1"})


def _snapshot(*, dimension_order: list[int] | None = None) -> dict:
    dimensions = [
        {"id": 2, "label": "成长空间", "value_text": None},
        {"id": 1, "label": "通勤", "value_text": "地铁 35 分钟"},
    ]
    if dimension_order is not None:
        by_id = {item["id"]: item for item in dimensions}
        dimensions = [by_id[item] for item in dimension_order]
    return build_offer_negotiation_snapshot(
        offer={
            "id": 9,
            "company_name": "星云数据",
            "position_name": "后端工程师",
            "status": "pending",
            "base_monthly": 28000,
            "months_per_year": 12,
            "signing_bonus": 0,
            "equity": "期权待确认",
            "perks": "餐补",
            "deadline": "2026-09-01",
            "notes": "用户备注",
            "assessment": "不得进入快照",
        },
        dimensions=dimensions,
        user_brief={
            "goal": "争取固定月薪再增加 2K",
            "concerns": "担心提出后影响 Offer",
            "scenario": "电话沟通",
        },
        idempotency_key="A" * 16,
    )


def _item(item_id: str, template_id: str, *evidence_ref_ids: str) -> dict:
    return {
        "id": item_id,
        "template_id": template_id,
        "evidence_ref_ids": list(evidence_ref_ids),
    }


def _valid_payload() -> dict:
    return {
        "proposal_status": "normal",
        "communication_goals": [
            _item(
                "goal-1",
                "goal_interest_then_request",
                "offer.company_name",
                "offer.position_name",
                "brief.goal",
            )
        ],
        "clarification_questions": [_item("question-1", "ask_concern_details", "brief.concerns")],
        "talking_points": [
            _item(
                "point-1",
                "say_current_offer_and_request",
                "offer.base_monthly",
                "offer.months_per_year",
                "brief.goal",
            )
        ],
        "preparation_checks": [
            _item("check-1", "check_goal_and_concern", "brief.goal", "brief.concerns")
        ],
    }


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_snapshot_dimension_order_is_canonical_and_missing_values_have_no_evidence_path() -> None:
    first = _snapshot(dimension_order=[2, 1])
    second = _snapshot(dimension_order=[1, 2])
    assert first == second
    assert first["offer_snapshot"]["dimensions"] == [
        {"path_id": "dimension_001", "label": "通勤", "value_text": "地铁 35 分钟"},
        {"path_id": "dimension_002", "label": "成长空间", "value_text": None},
    ]


def test_snapshot_uses_versioned_offer_fields_without_assessment() -> None:
    snapshot = _snapshot()
    assert snapshot["snapshot_version"] == 1
    assert snapshot["offer_snapshot"]["status"] == "pending"
    assert "assessment" not in snapshot["offer_snapshot"]


def test_provider_projection_omits_missing_dimension_label_and_value() -> None:
    model = FakeModel([_json(_valid_payload())])
    generate_offer_negotiation_proposal(model, _snapshot())
    prompt = "\n".join(message.content for message in model.calls[0][0])
    assert '"value_text":null' not in prompt
    assert '"label":"成长空间"' not in prompt


def test_provider_receives_stable_evidence_ids_and_no_database_ids() -> None:
    model = FakeModel([_json(_valid_payload())])
    generate_offer_negotiation_proposal(model, _snapshot())
    prompt = "\n".join(message.content for message in model.calls[0][0])
    assert '"evidence_id":"offer.base_monthly"' in prompt
    assert '"evidence_id":"brief.goal"' in prompt
    assert '"evidence_id":"offer.dimension.dimension_001"' in prompt
    assert '"id":9' not in prompt


def test_provider_receives_only_evidence_referenced_by_available_templates() -> None:
    snapshot = _snapshot()
    snapshot["offer_snapshot"]["notes"] = "PRIVATE-NOTE-CANARY"
    model = FakeModel([_json(_valid_payload())])
    generate_offer_negotiation_proposal(model, snapshot)
    prompt = "\n".join(message.content for message in model.calls[0][0])
    assert '"evidence_id":"offer.status"' not in prompt
    assert '"evidence_id":"offer.notes"' not in prompt
    assert "PRIVATE-NOTE-CANARY" not in prompt
    assert '"evidence_id":"offer.base_monthly"' in prompt
    assert '"evidence_id":"brief.goal"' in prompt


def test_evidence_catalog_omits_blank_values_but_preserves_nonblank_raw_text() -> None:
    snapshot = _snapshot()
    snapshot["offer_snapshot"]["notes"] = " \t"
    snapshot["offer_snapshot"]["equity"] = "   "
    snapshot["offer_snapshot"]["perks"] = "\n"
    snapshot["offer_snapshot"]["deadline"] = "  "
    snapshot["offer_snapshot"]["dimensions"][1]["value_text"] = " \t"
    model = FakeModel([_json(_valid_payload())])
    generate_offer_negotiation_proposal(model, snapshot)
    catalog = model.calls[0][0][0].content.split("evidence_catalog", 1)[1]
    assert '"evidence_id":"offer.notes"' not in catalog
    assert '"evidence_id":"offer.equity"' not in catalog
    assert '"evidence_id":"offer.perks"' not in catalog
    assert '"evidence_id":"offer.deadline"' not in catalog
    assert "dimension_002" not in catalog
    assert "地铁 35 分钟" in catalog


def test_server_renders_an_actionable_kit_from_only_verified_values() -> None:
    result = validate_offer_negotiation(_valid_payload(), _snapshot())
    goal = result["communication_goals"][0]
    question = result["clarification_questions"][0]
    script = result["talking_points"][0]
    check = result["preparation_checks"][0]

    assert "星云数据" in goal["text"]
    assert "后端工程师" in goal["text"]
    assert "争取固定月薪再增加 2K" in goal["text"]
    assert "担心提出后影响 Offer" in question["text"]
    assert "¥28,000/月 × 12 薪" in script["text"]
    assert "可以接受的底线" in check["text"]
    assert all(
        set(item) == {"id", "text", "rationale", "evidence_refs"}
        for field in OFFER_NEGOTIATION_FIELDS[1:]
        for item in result[field]
    )
    assert all("该建议由系统" not in item["rationale"] for item in (goal, question, script, check))


def test_every_catalog_template_has_a_server_renderer_and_public_shape() -> None:
    snapshot = _snapshot()
    catalog = build_template_catalog(snapshot)
    expected_template_ids = {
        "goal_focus_request",
        "goal_interest_then_request",
        "ask_current_compensation_structure",
        "ask_request_flexibility",
        "ask_concern_details",
        "ask_offer_validity_after_request",
        "ask_signing_bonus_terms",
        "ask_equity_terms",
        "ask_benefit_terms",
        "ask_decision_deadline",
        "ask_comparison_dimension",
        "say_interest_and_request",
        "say_current_offer_and_request",
        "say_concern_and_request",
        "say_request_and_preserve_offer",
        "say_scenario_opening",
        "check_current_compensation",
        "check_goal_and_concern",
        "check_decision_deadline",
        "check_written_follow_up",
        "check_comparison_dimension",
    }
    assert set(TEMPLATE_IDS) == expected_template_ids
    assert {option.template_id for option in catalog} == expected_template_ids

    for index, option in enumerate(catalog):
        payload = {
            "proposal_status": "normal",
            **{field: [] for field in OFFER_NEGOTIATION_FIELDS[1:]},
        }
        payload[option.section] = [
            _item(f"catalog-{index}", option.template_id, *option.evidence_ref_ids)
        ]
        result = validate_offer_negotiation(payload, snapshot)
        rendered = result[option.section][0]
        assert set(rendered) == {"id", "text", "rationale", "evidence_refs"}
        assert rendered["text"].strip()
        assert rendered["rationale"].strip()
        assert [ref["path"] for ref in rendered["evidence_refs"]]
        assert "template_id" not in rendered
        assert "evidence_ref_ids" not in rendered


def test_same_template_changes_with_the_frozen_goal_and_offer_facts() -> None:
    first = validate_offer_negotiation(_valid_payload(), _snapshot())["talking_points"][0]["text"]
    changed = _snapshot()
    changed["offer_snapshot"]["base_monthly"] = 24000
    changed["offer_snapshot"]["months_per_year"] = 16
    changed["user_brief"]["goal"] = "希望能多 2K"
    second = validate_offer_negotiation(_valid_payload(), changed)["talking_points"][0]["text"]
    assert first != second
    assert "¥24,000/月 × 16 薪" in second
    assert "希望能多 2K" in second


def test_offer_cancellation_concern_can_select_a_direct_safe_script() -> None:
    payload = _valid_payload()
    payload["clarification_questions"][0] = _item(
        "question-1",
        "ask_offer_validity_after_request",
        "brief.concerns",
    )
    payload["talking_points"][0] = _item(
        "point-1",
        "say_request_and_preserve_offer",
        "brief.goal",
        "brief.concerns",
    )
    result = validate_offer_negotiation(payload, _snapshot())
    assert "当前已经发出的 Offer 是否仍然有效" in result["clarification_questions"][0]["text"]
    assert "原回复截止时间是否保持不变" in result["talking_points"][0]["text"]
    assert "争取固定月薪再增加 2K" in result["talking_points"][0]["text"]


def test_interest_template_does_not_mislabel_a_non_salary_goal() -> None:
    snapshot = _snapshot()
    snapshot["user_brief"]["goal"] = "确认远程办公安排"
    payload = _valid_payload()
    payload["talking_points"][0] = _item(
        "point-1",
        "say_interest_and_request",
        "offer.company_name",
        "offer.position_name",
        "brief.goal",
    )
    text = validate_offer_negotiation(payload, snapshot)["talking_points"][0]["text"]
    assert "关于这次沟通" in text
    assert "关于薪酬" not in text


def test_provider_cannot_supply_free_form_text_or_rationale() -> None:
    for field in ("text", "rationale", "intent", "topic", "evidence_refs"):
        invalid = _valid_payload()
        invalid["communication_goals"][0][field] = "建议接受这份 Offer。"
        with pytest.raises(OfferNegotiationModelError) as error:
            validate_offer_negotiation(invalid, _snapshot())
        assert error.value.validation_category == "invalid_item_shape"


def test_unknown_evidence_id_is_semantic_and_not_repaired() -> None:
    invalid = _valid_payload()
    invalid["communication_goals"][0]["evidence_ref_ids"][0] = "attacker.secret"
    model = FakeModel([_json(invalid), _json(_valid_payload())])
    with pytest.raises(OfferNegotiationModelError) as error:
        generate_offer_negotiation_proposal(model, _snapshot())
    assert error.value.validation_category == "unknown_evidence_ref"
    assert len(model.calls) == 1
    assert error.value.provider_request_id.startswith("request-redacted-")


def test_template_must_belong_to_its_section() -> None:
    invalid = _valid_payload()
    invalid["communication_goals"][0] = _item(
        "goal-1",
        "say_current_offer_and_request",
        "offer.base_monthly",
        "offer.months_per_year",
        "brief.goal",
    )
    with pytest.raises(OfferNegotiationModelError) as error:
        validate_offer_negotiation(invalid, _snapshot())
    assert error.value.validation_category == "template_section_mismatch"


def test_template_requires_the_exact_evidence_set() -> None:
    invalid = _valid_payload()
    invalid["talking_points"][0]["evidence_ref_ids"] = ["brief.goal"]
    with pytest.raises(OfferNegotiationModelError) as error:
        validate_offer_negotiation(invalid, _snapshot())
    assert error.value.validation_category == "template_evidence_mismatch"


def test_template_cannot_use_a_missing_offer_fact() -> None:
    snapshot = _snapshot()
    snapshot["offer_snapshot"]["deadline"] = None
    payload = _valid_payload()
    payload["clarification_questions"][0] = _item(
        "question-1", "ask_decision_deadline", "offer.deadline"
    )
    with pytest.raises(OfferNegotiationModelError) as error:
        validate_offer_negotiation(payload, snapshot)
    assert error.value.validation_category == "unknown_evidence_ref"


def test_dynamic_dimension_template_uses_its_label_and_value_server_side() -> None:
    payload = _valid_payload()
    payload["clarification_questions"][0] = _item(
        "question-1",
        "ask_comparison_dimension",
        "offer.dimension.dimension_001",
    )
    result = validate_offer_negotiation(payload, _snapshot())
    question = result["clarification_questions"][0]
    assert "通勤" in question["text"]
    assert "地铁 35 分钟" in question["text"]
    assert question["evidence_refs"] == [
        {
            "source": "offer_snapshot",
            "path": "/offer_snapshot/dimensions/dimension_001/value_text",
            "excerpt": "地铁 35 分钟",
        }
    ]


def test_duplicate_template_selection_is_rejected_even_with_different_item_ids() -> None:
    invalid = _valid_payload()
    invalid["communication_goals"].append({**invalid["communication_goals"][0], "id": "goal-2"})
    with pytest.raises(OfferNegotiationModelError) as error:
        validate_offer_negotiation(invalid, _snapshot())
    assert error.value.validation_category == "duplicate_template"


def test_structure_failure_repairs_once_and_uses_the_same_closed_dsl() -> None:
    model = FakeModel(["{", _json(_valid_payload())])
    result = generate_offer_negotiation_proposal(model, _snapshot())
    assert result["proposal_status"] == "normal"
    assert len(model.calls) == 2
    assert "争取固定月薪再增加 2K" in result["communication_goals"][0]["text"]
    assert '"text"' not in model.calls[1][0][1].content


def test_unknown_template_id_is_repaired_once_as_a_shape_error() -> None:
    invalid = _valid_payload()
    invalid["communication_goals"][0]["template_id"] = "invented_template"
    model = FakeModel([_json(invalid), _json(_valid_payload())])
    result = generate_offer_negotiation_proposal(model, _snapshot())
    assert result["proposal_status"] == "normal"
    assert len(model.calls) == 2


@pytest.mark.parametrize("bad_refs", ["not-an-array", [], [1], ["brief.goal"] * 5])
def test_evidence_id_shape_is_repaired_or_stopped_by_the_limit(bad_refs: object) -> None:
    invalid = _valid_payload()
    invalid["communication_goals"][0]["evidence_ref_ids"] = bad_refs
    model = FakeModel([_json(invalid), _json(_valid_payload())])
    if isinstance(bad_refs, list) and len(bad_refs) > 4:
        with pytest.raises(OfferNegotiationModelError) as error:
            generate_offer_negotiation_proposal(model, _snapshot())
        assert error.value.validation_category == "limit_exceeded"
        assert len(model.calls) == 1
    else:
        result = generate_offer_negotiation_proposal(model, _snapshot())
        assert result["proposal_status"] == "normal"
        assert len(model.calls) == 2


def test_provider_item_count_is_bounded_to_three_per_section() -> None:
    invalid = _valid_payload()
    invalid["communication_goals"] = [
        _item(f"goal-{index}", "goal_focus_request", "brief.goal") for index in range(4)
    ]
    model = FakeModel([_json(invalid), _json(_valid_payload())])
    with pytest.raises(OfferNegotiationModelError) as error:
        generate_offer_negotiation_proposal(model, _snapshot())
    assert error.value.validation_category == "limit_exceeded"
    assert len(model.calls) == 1


def test_generation_prompt_declares_templates_and_evidence_ids_only() -> None:
    model = FakeModel([_json(_valid_payload())])
    generate_offer_negotiation_proposal(model, _snapshot())
    prompt = "\n".join(message.content for message in model.calls[0][0])
    assert "template_catalog" in prompt
    assert "evidence_ref_ids" in prompt
    assert "goal_interest_then_request" in prompt
    assert '"topic":' not in prompt
    assert "模型不得返回 text" in prompt


def test_native_schema_uses_the_same_closed_provider_contract() -> None:
    model = FakeModel([_json(_valid_payload())])
    model.supports_json_schema = True
    generate_offer_negotiation_proposal(model, _snapshot())
    schema = model.calls[0][1]["json_schema"]["schema"]
    item_schema = schema["properties"]["communication_goals"]["items"]
    assert item_schema["required"] == ["id", "template_id", "evidence_ref_ids"]
    assert item_schema["properties"]["template_id"]["enum"]
    assert "topic" not in item_schema["properties"]
    assert "text" not in item_schema["properties"]
    assert "rationale" not in item_schema["properties"]


def test_safe_empty_has_exact_four_empty_arrays() -> None:
    empty = safe_empty_offer_negotiation_proposal()
    assert set(empty) == set(OFFER_NEGOTIATION_FIELDS)
    assert all(value == [] for key, value in empty.items() if key != "proposal_status")
    assert empty["proposal_status"] == "safe_empty"


def test_safe_empty_repair_emits_only_redacted_diagnostic() -> None:
    diagnostics: list[dict[str, object]] = []
    model = FakeModel(["{", "{"])
    result = generate_offer_negotiation_proposal(
        model, _snapshot(), on_diagnostic=diagnostics.append
    )
    assert result["proposal_status"] == "safe_empty"
    assert len(diagnostics) == 1
    assert diagnostics[0]["failure_category"] == "invalid_json"
    assert diagnostics[0]["repair_attempted"] is True
    assert diagnostics[0]["repair_count"] == 1
    assert diagnostics[0]["provider_request_id"].startswith("request-redacted-")


@pytest.mark.parametrize("field", ["goal", "concerns", "scenario"])
def test_snapshot_rejects_blank_user_brief(field: str) -> None:
    brief = {"goal": "目标", "concerns": "顾虑", "scenario": "电话"}
    brief[field] = " \t"
    with pytest.raises(ValueError):
        build_offer_negotiation_snapshot(
            offer={"company_name": "公司", "position_name": "职位"},
            dimensions=[],
            user_brief=brief,
            idempotency_key="A" * 16,
        )


def test_limit_exceeded_id_is_terminal_not_repairable() -> None:
    invalid = _valid_payload()
    invalid["communication_goals"][0]["id"] = "x" * 65
    model = FakeModel([_json(invalid), _json(_valid_payload())])
    with pytest.raises(OfferNegotiationModelError) as error:
        generate_offer_negotiation_proposal(model, _snapshot())
    assert error.value.validation_category == "limit_exceeded"
    assert len(model.calls) == 1
