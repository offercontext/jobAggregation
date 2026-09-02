from __future__ import annotations

from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from domain_harness import execute_case
from golden import load_golden

from offerpilot.ai.tool_runtime.metadata import freeze_json
from offerpilot.ai.tool_specs.common import offer_json
from offerpilot.db import init_database
from offerpilot.models import Application, Offer
from offerpilot.pilot_runtime.compensation import (
    CompensationConflictError,
    execute_delete_offer_undo,
)
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.offers import OfferCreate, OffersRepository
from offerpilot.ai.tool_runtime.contracts import materialize_provider_payloads
from offerpilot.ai.tool_specs.offers import offer_specs


OFFER_TOOLS = (
    "list_offers",
    "get_offer",
    "compare_offers",
    "create_offer",
    "update_offer",
    "save_offer_assessment",
)


def test_create_offer_is_provider_visible_and_bound_to_application() -> None:
    specs = offer_specs()
    create = next((spec for spec in specs if spec.name == "create_offer"), None)

    assert create is not None
    assert create.contract.parameters["required"] == ["application_id"]
    assert create.metadata.domains == ("offers",)
    assert create.metadata.required_capabilities == ("offers.write",)
    assert create.metadata.binding.contract.kind == "enforce_if_bound"
    assert create.metadata.binding.contract.entity_kind == "application"
    assert create.metadata.confirmation_policy == "required"
    assert create.metadata.operation.undo_payload_kind.value == "delete_offer"
    assert create.metadata.operation.compensation_kind.value == "undo:create_offer"
    assert create.metadata.operation.undo_builder_id == "create_offer_delete_v1"


def test_create_offer_pending_projection_is_generic_and_does_not_read_repository() -> None:
    create = next(spec for spec in offer_specs() if spec.name == "create_offer")
    arguments = create.decoder(
        {
            "application_id": 1,
            "base_monthly": 24_000,
            "months_per_year": 16,
        }
    )

    class ForbiddenApplications:
        def get(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("Pending projection must not read applications")

    details = create.presentation.pending_details_projector(
        arguments,
        SimpleNamespace(applications=ForbiddenApplications()),
    )

    assert details == {
        "target": {
            "id": "offer-draft-1",
            "kind": "offer",
            "title": "新建 Offer",
            "meta": "",
            "source": "pending_action",
        },
        "proposed_changes": [
            {"field": "base_monthly", "before": "", "after": 24_000},
            {"field": "months_per_year", "before": "", "after": 16},
        ],
        "evidence": [
            {
                "id": "offer-draft-1",
                "kind": "offer",
                "title": "新建 Offer",
                "meta": "",
                "source": "pending_action",
            }
        ],
    }


def test_create_offer_persists_against_an_existing_application(tmp_path: Path) -> None:
    visible, projection, handler_calls = execute_case(
        offer_specs(),
        {
            "tool_name": "create_offer",
            "case": "success",
            "arguments": {
                "application_id": 1,
                "base_monthly": 24000,
                "months_per_year": 16,
                "deadline": "2026-09-15",
            },
            "business_projection": {"table": "offers", "row_count": 2},
        },
        tmp_path / "create-offer.db",
    )

    assert "created" in visible
    assert projection == {"table": "offers", "row_count": 2}
    assert handler_calls == 1


def test_create_offer_undo_deletes_only_the_exact_created_offer(tmp_path: Path) -> None:
    session_factory = init_database(tmp_path / "undo-offer.db")
    application = ApplicationsRepository(session_factory).create(
        ApplicationCreate(company_name="Alpha", position_name="Platform Engineer")
    )
    offer = OffersRepository(session_factory).create(
        OfferCreate(
            application_id=application.id,
            company_name=application.company_name,
            position_name=application.position_name,
            base_monthly=24000,
            months_per_year=16,
            deadline="2026-09-15",
        )
    )
    result = offer_json(offer)
    expected = {
        "application_id": result["application_id"],
        "company_name": result["company_name"],
        "position_name": result["position_name"],
        "status": result["status"],
        "base_monthly": result["base_monthly"],
        "months_per_year": result["months_per_year"],
        "signing_bonus": result["signing_bonus"],
        "equity": result["equity"],
        "perks": result["perks"],
        "deadline": result["deadline"],
        "notes": result["notes"],
        "assessment": result["assessment"],
        "total_cash": result["total_cash"],
        "created_at": result["created_at"].replace("+00:00", "Z"),
        "updated_at": result["updated_at"].replace("+00:00", "Z"),
    }
    undo = freeze_json(
        {
            "kind": "delete_offer",
            "label": "撤销新建 Offer",
            "offer_id": offer.id,
            "expected_after": expected,
        }
    )

    with session_factory() as session:
        inconsistent = freeze_json(
            {
                "kind": "delete_offer",
                "label": "撤销新建 Offer",
                "offer_id": offer.id,
                "expected_after": {**expected, "total_cash": expected["total_cash"] + 1},
            }
        )
        with pytest.raises(ValueError, match="total_cash is inconsistent"):
            execute_delete_offer_undo(session, inconsistent)
        assert session.scalar(select(Offer).where(Offer.id == offer.id)) is not None

        changed = session.get(Offer, offer.id)
        assert changed is not None
        changed.notes = "用户已经补充了谈薪备注"
        session.flush()
        with pytest.raises(CompensationConflictError, match="undo_conflict"):
            execute_delete_offer_undo(session, undo)
        session.rollback()

        assert execute_delete_offer_undo(session, undo) == "已撤销最近一次 AI 写入：新建 Offer 已删除。"
        session.commit()
        assert session.scalar(select(Offer).where(Offer.id == offer.id)) is None
        assert session.scalar(select(Application).where(Application.id == application.id)) is not None


def _cases() -> list[dict[str, Any]]:
    return [case for case in load_golden("tool_outcomes_30c944f.json")["cases"] if case["tool_name"] in OFFER_TOOLS]


def test_offer_specs_preserve_provider_contracts() -> None:
    specs = offer_specs()
    manifest = load_golden("provider_manifest_current.json")
    expected = [payload for payload in manifest["tools"] if payload["function"]["name"] in OFFER_TOOLS]
    assert tuple(spec.name for spec in specs) == OFFER_TOOLS
    assert materialize_provider_payloads(tuple(spec.contract for spec in specs)) == expected


@pytest.mark.parametrize("case", _cases(), ids=lambda case: f"{case['tool_name']}:{case['case']}")
def test_offer_spec_matches_baseline_case(case: dict[str, Any], tmp_path: Path) -> None:
    visible, projection, handler_calls = execute_case(
        offer_specs(), case, tmp_path / "case.db"
    )
    assert visible == case["visible_result"]
    assert handler_calls == case["handler_calls"]
    if case["business_projection"]:
        assert projection == {"table": case["business_projection"]["table"], "row_count": case["business_projection"]["row_count"]}
