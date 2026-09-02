from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from offerpilot.ai.tool_authority.policy import binding_policy_fingerprint
from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog


ROOT = Path(__file__).parents[1] / "fixtures"
METADATA = ROOT / "tool_metadata"
CURRENT_SOURCE_BASELINE = "create-offer-current-v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), raw


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def test_current_golden_index_pins_the_create_offer_surface() -> None:
    index, _ = _read(METADATA / "golden_index_current.json")
    assert index["source_baseline"] == CURRENT_SOURCE_BASELINE
    for item in index["assets"]:
        value, raw = _read(METADATA / item["name"])
        assert raw == (_canonical(value) + "\n").encode("utf-8")
        assert item["raw_sha256"] == _sha256(raw)
        assert item["canonical_sha256"] == _sha256(_canonical(value).encode("utf-8"))
    for item in index["independent_goldens"]:
        value, raw = _read(ROOT / item["path"])
        assert raw == (_canonical(value) + "\n").encode("utf-8")
        assert item["raw_sha256"] == _sha256(raw)
        assert item["canonical_sha256"] == _sha256(_canonical(value).encode("utf-8"))


def test_current_metadata_goldens_match_the_production_catalog() -> None:
    catalog = build_model_tool_catalog()
    manifest, _ = _read(METADATA / "tool_metadata_manifest_current.json")
    authority, _ = _read(ROOT / "tool_authority" / "authority_manifest_current.json")
    dependency, _ = _read(ROOT / "tool_authority" / "dependency_policy_current.json")
    policy, _ = _read(ROOT / "tool_authority" / "policy_fingerprints_current.json")
    actual = compile_tool_metadata_manifest(catalog.specs).to_dict()
    assert actual == manifest
    assert authority == {
        "schema_version": 1,
        "tools": [
            {
                "ordinal": item["ordinal"],
                "name": item["provider_name"],
                "kind": "write" if item["operation"]["kind"] == "transactional_write" else "read",
                "confirmation_policy": item["confirmation_policy"],
                "required_capabilities": item["required_capabilities"],
                "binding": item["binding"]["contract"],
                "resolvers": item["binding"]["resolver_descriptors"],
            }
            for item in manifest["typed_tools"]
        ],
    }
    expected_names = [item["provider_name"] for item in manifest["typed_tools"]]
    expected_dependencies = {
        item["provider_name"]: item["dependencies"] for item in manifest["typed_tools"]
    }
    assert dependency["catalog_names"] == expected_names
    assert dependency["coverage"] == 26
    assert dependency["dependencies"] == expected_dependencies
    dependency_input = {
        "dependency_policy_version": dependency["dependency_policy_version"],
        "catalog_names": dependency["catalog_names"],
        "dependencies": dependency["dependencies"],
    }
    assert dependency["canonical_sha256"] == _sha256(_canonical(dependency_input).encode("utf-8"))
    assert policy["binding_policy"]["fingerprint"] == binding_policy_fingerprint(authority)


def test_current_offer_tool_is_a_required_write_with_application_resolver_and_undo() -> None:
    manifest, _ = _read(METADATA / "tool_metadata_manifest_current.json")
    offer = next(item for item in manifest["typed_tools"] if item["provider_name"] == "create_offer")
    assert offer["ordinal"] == 17
    assert offer["required_capabilities"] == ["offers.write"]
    assert offer["confirmation_policy"] == "required"
    assert offer["binding"] == {
        "contract": {"kind": "enforce_if_bound", "entity_kind": "application"},
        "resolver_descriptors": [
            {
                "resolver_id": "application_identity_arg",
                "entity_kind": "application",
                "arg_path": "application_id",
                "presence": "required",
                "identity_type": "positive_int64",
            }
        ],
    }
    assert offer["operation"]["undo_policy"] == "required"
    assert offer["operation"]["undo_payload_kind"] == "delete_offer"
    assert offer["operation"]["compensation_kind"] == "undo:create_offer"
    assert offer["operation"]["undo_builder_id"] == "create_offer_delete_v1"
