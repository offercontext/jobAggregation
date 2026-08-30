from __future__ import annotations

import hashlib
import json

import pytest

from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog
from offerpilot.product_actions.catalog import (
    ProductActionCatalogV1,
    ProductActionCompensationCatalogV1,
)
from offerpilot.product_actions.contracts import (
    PRODUCT_ACTION_COMPENSATION_NAMES,
    PRODUCT_ACTION_NAMES,
    ProductActionProofRegistryV1,
)
from tests.product_actions.conftest import raw_json, signal_route


def test_catalogs_are_exact_independent_two_by_two_surfaces() -> None:
    registry = ProductActionProofRegistryV1()
    actions = ProductActionCatalogV1(registry)
    compensations = ProductActionCompensationCatalogV1()

    assert PRODUCT_ACTION_NAMES == (
        "confirm_interview_story",
        "save_review_readiness_signal",
    )
    assert PRODUCT_ACTION_COMPENSATION_NAMES == (
        "undo:confirm_interview_story",
        "undo:save_review_readiness_signal",
    )
    assert actions.names() == PRODUCT_ACTION_NAMES
    assert compensations.names() == PRODUCT_ACTION_COMPENSATION_NAMES
    assert len(actions.ordered_specs) == 2
    assert len(compensations.ordered_specs) == 2
    assert [spec.route_source for spec in actions.ordered_specs] == [
        "interview_story_owner",
        "review_readiness_focus_owner",
    ]
    assert [spec.capabilities for spec in actions.ordered_specs] == [
        ("stories.write",),
        ("application.interview_readiness_feedback.write",),
    ]
    assert all(spec.undo_policy == "required" for spec in actions.ordered_specs)
    assert all(not hasattr(spec, "execute") for spec in compensations.ordered_specs)


def test_product_action_catalog_fingerprint_is_static_and_cross_process_safe() -> None:
    registry = ProductActionProofRegistryV1()
    catalog = ProductActionCatalogV1(registry)

    assert catalog.fingerprint == (
        "sha256:41bc5dbf9d2a4255a954573989425bc2789f8430394bb6ab3fc2623589872a5d"
    )
    projection = catalog.metadata_projection()
    canonical = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert "sha256:" + hashlib.sha256(canonical).hexdigest() == catalog.fingerprint


def test_catalogs_and_specs_reject_mutation_and_cross_registry_resolution(
    product_core: tuple[object, ...],
) -> None:
    catalog, registry, _profiles, signal_issuer, _story_issuer = product_core
    assert isinstance(catalog, ProductActionCatalogV1)
    assert isinstance(registry, ProductActionProofRegistryV1)
    prepared = signal_issuer.prepare(route_payload_raw=raw_json(signal_route()))

    with pytest.raises(AttributeError, match="sealed"):
        catalog.fingerprint = "sha256:" + "0" * 64  # type: ignore[misc]
    with pytest.raises(ValueError, match="provenance|registry"):
        ProductActionCatalogV1(ProductActionProofRegistryV1()).resolve(
            prepared.route_proof,
            expected_binding=prepared.proof_binding,
        )
    first = catalog.ordered_specs[0]
    original = first.action_name
    try:
        object.__setattr__(first, "action_name", "other")
        with pytest.raises(
            (AttributeError, TypeError, ValueError),
            match="sealed|integrity|invalid",
        ):
            catalog.names()
    finally:
        object.__setattr__(first, "action_name", original)


def test_agent_provider_bytes_and_order_remain_exactly_the_pinned_25() -> None:
    fixture = json.loads(
        (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "fixtures"
            / "tool_pipeline"
            / "provider_manifest_30c944f.json"
        ).read_text(encoding="utf-8")
    )
    model_catalog = build_model_tool_catalog()

    assert len(model_catalog.specs) == 25
    assert model_catalog.materialize_provider_payloads() == fixture["tools"]
    assert tuple(spec.name for spec in model_catalog.specs) == tuple(
        item["function"]["name"] for item in fixture["tools"]
    )
    for name in PRODUCT_ACTION_NAMES:
        assert model_catalog.resolve(name) is None
