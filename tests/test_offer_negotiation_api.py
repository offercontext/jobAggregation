from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from offerpilot.ai.types import Assistant
from offerpilot.api import create_app


class FakeModel:
    supports_json_schema = False

    def __init__(self, responses: list[str] | None = None, error: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[list] = []

    def complete(self, messages, tools, response_format=None):
        self.calls.append([messages, response_format])
        if self.error is not None:
            raise self.error
        return Assistant(content=self.responses.pop(0), provider_blocks={"request_id": "req-1"})


class ProviderError(RuntimeError):
    provider_request_id = "provider-request-secret"
    status_code = 503


class ProviderTimeoutError(TimeoutError):
    provider_request_id = "provider-timeout-secret"


class NestedProviderError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("provider failed")
        self.diagnostic = {
            "provider_request_id": "nested-provider-secret",
            "http_status": 503,
            "timeout": True,
        }


def _offer(client: TestClient) -> dict:
    response = client.post(
        "/api/offers",
        json={
            "company_name": "星云数据",
            "position_name": "后端工程师",
            "base_monthly": 28000,
            "months_per_year": 12,
            "signing_bonus": 0,
        },
    )
    assert response.status_code == 201
    return response.json()


def _payload() -> dict:
    return {
        "proposal_status": "normal",
        "communication_goals": [
            {
                "id": "goal-1",
                "template_id": "goal_interest_then_request",
                "evidence_ref_ids": ["offer.company_name", "offer.position_name", "brief.goal"],
            }
        ],
        "clarification_questions": [
            {
                "id": "question-1",
                "template_id": "ask_request_flexibility",
                "evidence_ref_ids": ["brief.goal"],
            }
        ],
        "talking_points": [
            {
                "id": "point-1",
                "template_id": "say_current_offer_and_request",
                "evidence_ref_ids": ["offer.base_monthly", "offer.months_per_year", "brief.goal"],
            }
        ],
        "preparation_checks": [
            {
                "id": "check-1",
                "template_id": "check_goal_and_concern",
                "evidence_ref_ids": ["brief.goal", "brief.concerns"],
            }
        ],
    }


def _request(client: TestClient, key: str = "A" * 16) -> object:
    preview_payload = {
        "dimension_ids": [],
        "goal": "goal",
        "concerns": "concerns",
        "scenario": "scenario",
    }
    preview_fingerprint = client.post(
        "/api/offers/1/negotiation/preview", json=preview_payload
    ).json()["source_fingerprint"]
    return client.post(
        "/api/offers/1/negotiation/proposals",
        json={
            "idempotency_key": key,
            "source_fingerprint": preview_fingerprint,
            **preview_payload,
        },
    )


def test_generation_replay_is_immutable_and_payload_has_no_database_ids(tmp_path) -> None:
    model = FakeModel([json.dumps(_payload(), ensure_ascii=False)])
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _offer(client)
    response = _request(client)
    assert response.status_code == 201
    body = response.json()
    assert body["proposal_status"] == "normal"
    assert body["input_snapshot"]["snapshot_version"] == 1
    assert body["input_snapshot"]["offer_snapshot"]["company_name"] == "星云数据"
    assert '"id":' not in model.calls[0][0][1].content

    replay = _request(client)
    assert replay.status_code == 200
    assert replay.json()["id"] == body["id"]
    assert len(model.calls) == 1


def test_generation_requires_all_user_brief_fields(tmp_path) -> None:
    model = FakeModel([json.dumps(_payload(), ensure_ascii=False)])
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _offer(client)
    response = client.post(
        "/api/offers/1/negotiation/proposals",
        json={"idempotency_key": "M" * 16, "dimension_ids": [], "goal": "目标", "scenario": "电话"},
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "offer_negotiation_invalid_request"
    assert model.calls == []


def test_preview_invalid_request_uses_chinese_safe_message(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path, chat_model=FakeModel()))
    _offer(client)
    response = client.post(
        "/api/offers/1/negotiation/preview",
        json={"dimension_ids": [], "goal": "", "concerns": "顾虑", "scenario": "电话"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "谈薪准备输入无效"


@pytest.mark.parametrize("field", ["goal", "concerns", "scenario"])
def test_generation_rejects_blank_user_brief_fields(tmp_path, field: str) -> None:
    model = FakeModel([json.dumps(_payload(), ensure_ascii=False)])
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _offer(client)
    payload = {
        "idempotency_key": "W" * 16,
        "dimension_ids": [],
        "goal": "目标",
        "concerns": "顾虑",
        "scenario": "电话",
    }
    payload[field] = " \t"
    response = client.post("/api/offers/1/negotiation/proposals", json=payload)
    assert response.status_code == 422
    assert model.calls == []


def test_invalidated_attempts_are_not_in_history_list(tmp_path) -> None:
    invalid = _payload()
    invalid["communication_goals"][0]["evidence_ref_ids"][0] = "attacker.secret"
    model = FakeModel([json.dumps(invalid, ensure_ascii=False)])
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _offer(client)
    response = _request(client, "N" * 16)
    assert response.status_code == 502
    assert client.get("/api/offers/1/negotiation/proposals").json() == []


def test_semantic_failure_is_502_and_same_key_does_not_call_again(tmp_path) -> None:
    invalid = _payload()
    invalid["communication_goals"][0]["evidence_ref_ids"][0] = "attacker.secret"
    model = FakeModel(
        [json.dumps(invalid, ensure_ascii=False), json.dumps(_payload(), ensure_ascii=False)]
    )
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _offer(client)
    response = _request(client, "B" * 16)
    assert response.status_code == 502
    assert response.json()["error_code"] == "offer_negotiation_unverifiable"
    replay = _request(client, "B" * 16)
    assert replay.status_code == 502
    assert replay.json()["error_code"] == "offer_negotiation_unverifiable"
    assert len(model.calls) == 1


def test_provider_error_preserves_attempt_and_key(tmp_path) -> None:
    model = FakeModel(error=TimeoutError("provider timeout"))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _offer(client)
    response = _request(client, "C" * 16)
    assert response.status_code == 502
    assert response.json()["error_code"] == "offer_negotiation_provider_error"
    pending = _request(client, "C" * 16)
    assert pending.status_code == 202
    assert pending.json()["attempt_status"] == "provider_unknown"
    assert len(model.calls) == 1


def test_pending_history_includes_retry_after_ms(tmp_path) -> None:
    model = FakeModel(error=TimeoutError("provider timeout"))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _offer(client)
    response = _request(client, "Y" * 16)
    assert response.status_code == 502
    pending_response = _request(client, "Y" * 16)
    assert pending_response.status_code == 202
    pending = client.get(f"/api/offer-negotiation/proposals/{pending_response.json()['id']}")
    assert pending.status_code == 200
    assert pending.json()["attempt_status"] == "provider_unknown"
    assert pending.json()["retry_after_ms"] == 1000


def test_provider_diagnostic_keeps_only_hashed_request_id(tmp_path) -> None:
    model = FakeModel(error=ProviderError("provider timeout"))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _offer(client)
    response = _request(client, "R" * 16)
    assert response.status_code == 502
    messages = [entry["message"] for entry in client.get("/api/logs?limit=20").json()["entries"]]
    failure = next(
        message for message in messages if message.startswith("offer_negotiation_diagnostic")
    )
    assert "provider-request-secret" not in failure
    assert "request-redacted-" in failure
    assert "repair_count" in failure
    assert '"http_status":503' in failure
    assert '"timeout":false' in failure


def test_provider_diagnostic_marks_timeout_without_status(tmp_path) -> None:
    model = FakeModel(error=ProviderTimeoutError("provider timeout"))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _offer(client)
    response = _request(client, "T" * 16)
    assert response.status_code == 502
    entries = client.get("/api/logs?limit=50").json()["entries"]
    failure = next(
        entry["message"]
        for entry in entries
        if entry["message"].startswith("offer_negotiation_diagnostic ")
    )
    assert '"http_status":null' in failure
    assert '"timeout":true' in failure
    assert "provider-timeout-secret" not in failure


def test_provider_diagnostic_reads_nested_provider_diagnostic(tmp_path) -> None:
    model = FakeModel(error=NestedProviderError())
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _offer(client)
    response = _request(client, "U" * 16)
    assert response.status_code == 502
    entries = client.get("/api/logs?limit=50").json()["entries"]
    failure = next(
        entry["message"]
        for entry in entries
        if entry["message"].startswith("offer_negotiation_diagnostic ")
    )
    assert '"http_status":503' in failure
    assert '"timeout":true' in failure
    assert "nested-provider-secret" not in failure
    assert "request-redacted-" in failure


def test_generation_rejects_stale_preview_fingerprint_before_provider(tmp_path) -> None:
    model = FakeModel([json.dumps(_payload(), ensure_ascii=False)])
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    offer = _offer(client)
    brief = {"goal": "浜夊彇鍏ヨ亴鏃堕棿", "concerns": "閫氬嫟", "scenario": "鐢佃瘽娌熼€?"}
    preview = client.post(
        f"/api/offers/{offer['id']}/negotiation/preview",
        json={"dimension_ids": [], **brief},
    )
    assert preview.status_code == 200
    client.put(
        f"/api/offers/{offer['id']}",
        json={
            "company_name": offer["company_name"],
            "position_name": offer["position_name"],
            "base_monthly": 29000,
        },
    )
    response = client.post(
        f"/api/offers/{offer['id']}/negotiation/proposals",
        json={
            "idempotency_key": "V" * 16,
            "dimension_ids": [],
            **brief,
            "source_fingerprint": preview.json()["source_fingerprint"],
        },
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "offer_negotiation_source_changed"
    assert model.calls == []


def test_history_is_readable_after_offer_delete(tmp_path) -> None:
    model = FakeModel([json.dumps(_payload(), ensure_ascii=False)])
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    offer = _offer(client)
    response = _request(client, "D" * 16)
    proposal_id = response.json()["id"]
    assert client.delete(f"/api/offers/{offer['id']}").status_code in {200, 204}
    history = client.get(f"/api/offer-negotiation/proposals/{proposal_id}")
    assert history.status_code == 200
    assert history.json()["source_changed"] is True
    listed = client.get(f"/api/offers/{offer['id']}/negotiation/proposals")
    assert listed.status_code == 200
    assert listed.json()[0]["source_changed"] is True


def test_dimension_value_change_marks_history_source_changed(tmp_path) -> None:
    model = FakeModel([json.dumps(_payload(), ensure_ascii=False)])
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    offer = _offer(client)
    dimension = client.post("/api/offers/comparison-dimensions", json={"label": "通勤"}).json()
    client.put(
        f"/api/offers/{offer['id']}/comparison-values/{dimension['id']}",
        json={"value_text": "地铁 35 分钟"},
    )
    generated = client.post(
        f"/api/offers/{offer['id']}/negotiation/proposals",
        json={
            "idempotency_key": "J" * 16,
            "source_fingerprint": client.post(
                f"/api/offers/{offer['id']}/negotiation/preview",
                json={
                    "dimension_ids": [dimension["id"]],
                    "goal": "goal",
                    "concerns": "concerns",
                    "scenario": "scenario",
                },
            ).json()["source_fingerprint"],
            "dimension_ids": [dimension["id"]],
            "goal": "goal",
            "concerns": "concerns",
            "scenario": "scenario",
        },
    )
    assert generated.status_code == 201
    proposal_id = generated.json()["id"]
    client.put(
        f"/api/offers/{offer['id']}/comparison-values/{dimension['id']}",
        json={"value_text": "公交 50 分钟"},
    )
    history = client.get(f"/api/offer-negotiation/proposals/{proposal_id}")
    assert history.status_code == 200
    assert history.json()["source_changed"] is True


def test_offer_status_change_marks_frozen_negotiation_source_changed(tmp_path) -> None:
    model = FakeModel([json.dumps(_payload(), ensure_ascii=False)])
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    offer = _offer(client)
    generated = _request(client, "S" * 16)
    assert generated.status_code == 201
    updated = client.put(
        f"/api/offers/{offer['id']}",
        json={
            "company_name": offer["company_name"],
            "position_name": offer["position_name"],
            "base_monthly": offer["base_monthly"],
            "months_per_year": offer["months_per_year"],
            "signing_bonus": offer["signing_bonus"],
            "status": "negotiating",
        },
    )
    assert updated.status_code == 200
    history = client.get(f"/api/offer-negotiation/proposals/{generated.json()['id']}")
    assert history.status_code == 200
    assert history.json()["source_changed"] is True


def test_confirmation_is_hitl_idempotent_and_history_retains_brief(tmp_path) -> None:
    model = FakeModel([json.dumps(_payload(), ensure_ascii=False)])
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    offer = _offer(client)
    generated = _request(client, "E" * 16)
    proposal_id = generated.json()["id"]
    confirm_payload = {
        "confirmation_key": "F" * 16,
        "selected_blocks": ["goal-1", "point-1"],
        "edited_content": {"goal-1": "我想确认入职时间"},
    }
    confirmed = client.post(
        f"/api/offer-negotiation/proposals/{proposal_id}/confirm", json=confirm_payload
    )
    assert confirmed.status_code == 201
    replay = client.post(
        f"/api/offer-negotiation/proposals/{proposal_id}/confirm", json=confirm_payload
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == confirmed.json()["id"]
    assert (
        client.put(
            f"/api/offers/{offer['id']}",
            json={
                "company_name": "星云数据",
                "position_name": "后端工程师",
                "base_monthly": 28000,
                "months_per_year": 12,
                "signing_bonus": 0,
                "notes": "更新后的备注",
            },
        ).status_code
        == 200
    )
    history = client.get(f"/api/offer-negotiation/proposals/{proposal_id}")
    assert history.status_code == 200
    assert history.json()["source_changed"] is True
    assert history.json()["brief"]["selected_blocks"] == ["goal-1", "point-1"]
