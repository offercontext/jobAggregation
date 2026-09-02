from __future__ import annotations

import json

from offerpilot.ai.offer_negotiation import (
    build_offer_negotiation_snapshot,
    generate_offer_negotiation_proposal,
)
from offerpilot.ai.types import Assistant


class FakeModel:
    supports_json_schema = False

    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[list] = []

    def complete(self, messages, tools, response_format=None):
        self.calls.append([messages, response_format])
        return Assistant(content=json.dumps(self.response, ensure_ascii=False))


def _snapshot() -> dict:
    return build_offer_negotiation_snapshot(
        offer={
            "company_name": "星云数据",
            "position_name": "后端工程师",
            "status": "pending",
            "base_monthly": 28000,
            "months_per_year": 12,
            "signing_bonus": 0,
        },
        dimensions=[],
        user_brief={"goal": "争取入职时间", "concerns": "通勤", "scenario": "电话沟通"},
        idempotency_key="A" * 16,
    )


def test_provider_intent_is_rendered_by_server_templates() -> None:
    payload = {
        "proposal_status": "normal",
        "communication_goals": [
            {
                "id": "goal-1",
                "template_id": "goal_focus_request",
                "evidence_ref_ids": ["brief.goal"],
            }
        ],
        "clarification_questions": [],
        "talking_points": [],
        "preparation_checks": [],
    }
    result = generate_offer_negotiation_proposal(FakeModel(payload), _snapshot())
    item = result["communication_goals"][0]
    assert set(item) == {"id", "text", "rationale", "evidence_refs"}
    assert "争取入职时间" in item["text"] or "谈薪目标" in item["text"]
    assert "text" not in json.dumps(payload, ensure_ascii=False)


def test_provider_cannot_supply_free_form_decision_text() -> None:
    payload = {
        "proposal_status": "normal",
        "communication_goals": [
            {
                "id": "goal-1",
                "intent": "prepare_request",
                "template_id": "goal_focus_request",
                "text": "建议接受这份 Offer。",
                "evidence_ref_ids": ["brief.goal"],
            }
        ],
        "clarification_questions": [],
        "talking_points": [],
        "preparation_checks": [],
    }
    diagnostics: list[dict] = []
    try:
        generate_offer_negotiation_proposal(
            FakeModel(payload), _snapshot(), on_diagnostic=diagnostics.append
        )
    except ValueError:
        pass
    assert diagnostics[0]["failure_category"] in {"unexpected_field", "invalid_item_shape"}
