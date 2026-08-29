from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from offerpilot.ai.tool_runtime.protocol_seals import (
    APPROVED_LEGACY_DETERMINISTIC_BOUNDARY_V1,
    APPROVED_PROVIDER_TOOL_BOUNDARY_V1,
    verify_legacy_boundary,
    verify_provider_boundary,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"
PROVIDER_FIXTURE = FIXTURES / "tool_pipeline" / "provider_manifest_30c944f.json"
METADATA_FIXTURE = FIXTURES / "tool_metadata" / "tool_metadata_manifest_v1.json"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _provider_payloads() -> list[dict[str, Any]]:
    value = json.loads(PROVIDER_FIXTURE.read_bytes().decode("utf-8"))
    return copy.deepcopy(value["tools"])


def _legacy_fields() -> tuple[list[str], str, str]:
    value = json.loads(METADATA_FIXTURE.read_bytes().decode("utf-8"))
    boundary = value["legacy_boundary"]
    return (
        list(boundary["ordered_names"]),
        boundary["provider_visibility"],
        boundary["adapter_kind"],
    )


def _provider_digest(payloads: list[dict[str, Any]]) -> str:
    input_value = {
        "schema": "provider-tool-boundary-v1",
        "ordered_tools": payloads,
    }
    return "sha256:" + hashlib.sha256(_canonical_bytes(input_value)).hexdigest()


def _legacy_digest(names: list[str], visibility: str, adapter_kind: str) -> str:
    input_value = {
        "schema": "legacy-deterministic-boundary-v1",
        "ordered_names": names,
        "provider_visibility": visibility,
        "adapter_kind": adapter_kind,
    }
    return "sha256:" + hashlib.sha256(_canonical_bytes(input_value)).hexdigest()


def test_provider_boundary_seal_is_recomputed_from_full_ordered_baseline_payload() -> None:
    payloads = _provider_payloads()
    assert len(payloads) == 25
    assert _provider_digest(payloads) == APPROVED_PROVIDER_TOOL_BOUNDARY_V1
    assert verify_provider_boundary(payloads) is None


def test_legacy_boundary_seal_is_recomputed_from_ordered_baseline_boundary() -> None:
    names, visibility, adapter_kind = _legacy_fields()
    assert len(names) == 3
    assert _legacy_digest(names, visibility, adapter_kind) == (
        APPROVED_LEGACY_DETERMINISTIC_BOUNDARY_V1
    )
    assert verify_legacy_boundary(names, visibility, adapter_kind) is None


@pytest.mark.parametrize("mutation", ("name", "order", "payload"))
def test_provider_boundary_rejects_name_order_and_full_payload_mutations(
    mutation: str,
) -> None:
    payloads = _provider_payloads()
    if mutation == "name":
        payloads[0]["function"]["name"] = "renamed_provider_tool"
    elif mutation == "order":
        payloads[0], payloads[1] = payloads[1], payloads[0]
    else:
        payloads[0]["function"]["parameters"]["properties"]["status"]["description"] = (
            "mutated provider schema description"
        )

    assert _provider_digest(payloads) != APPROVED_PROVIDER_TOOL_BOUNDARY_V1
    with pytest.raises((TypeError, ValueError), match="provider|boundary|seal|digest"):
        verify_provider_boundary(payloads)


@pytest.mark.parametrize("mutation", ("name", "order", "visibility", "kind"))
def test_legacy_boundary_rejects_name_order_visibility_and_kind_mutations(
    mutation: str,
) -> None:
    names, visibility, adapter_kind = _legacy_fields()
    if mutation == "name":
        names[0] = "renamed_legacy_tool"
    elif mutation == "order":
        names[0], names[1] = names[1], names[0]
    elif mutation == "visibility":
        visibility = "model_eligible"
    else:
        adapter_kind = "typed"

    assert _legacy_digest(names, visibility, adapter_kind) != (
        APPROVED_LEGACY_DETERMINISTIC_BOUNDARY_V1
    )
    with pytest.raises((TypeError, ValueError), match="legacy|boundary|seal|digest"):
        verify_legacy_boundary(names, visibility, adapter_kind)


def test_protocol_seal_expected_digest_mutation_fails_closed() -> None:
    with pytest.raises((TypeError, ValueError), match="digest|seal|approved"):
        verify_provider_boundary(
            _provider_payloads(),
            expected_digest="sha256:" + "0" * 64,
        )
    names, visibility, adapter_kind = _legacy_fields()
    with pytest.raises((TypeError, ValueError), match="digest|seal|approved"):
        verify_legacy_boundary(
            names,
            visibility,
            adapter_kind,
            expected_digest="sha256:" + "0" * 64,
        )


def test_protocol_seal_inputs_do_not_expose_single_tool_lookup() -> None:
    module_source = (
        Path(__file__).parents[2]
        / "src"
        / "offerpilot"
        / "ai"
        / "tool_runtime"
        / "protocol_seals.py"
    )
    source = module_source.read_text(encoding="utf-8")
    assert "def provider_tool" not in source
    assert "def legacy_tool" not in source
    assert "resolve(" not in source
