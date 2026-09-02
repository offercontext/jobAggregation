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
CURRENT_GOLDEN_HASHES = {
    "tool_metadata/tool_metadata_manifest_current.json": {
        "raw": "sha256:3052c2735c58502f0e4ca090a136189d647eb989612079e6bfd8a9518bfe668b",
        "canonical": "sha256:d3c23a78f36e57f5353492f21e45a08f8a2f4d80bc3f959e2661be7898b24c1a",
    },
    "tool_authority/authority_manifest_current.json": {
        "raw": "sha256:e2c6b36b4a9b50ec810853abebbfc0bca66d670a6aca6c01a51b4674cbef29d5",
        "canonical": "sha256:5420110e18a053c7bd837abc798231b157d8d68f077ea40ab774ad16430194a0",
    },
    "tool_metadata/tool_selection_matrix_current.json": {
        "raw": "sha256:6414f27b09d9bd01cb334aac464c94c34c104bd73c1bf334850ff3c682021f28",
        "canonical": "sha256:30bd80d058345068c8ebe9e8f7454b0d4e3a70f2afb16b28c535f5f09c7ac352",
    },
    "tool_metadata/tool_operation_matrix_current.json": {
        "raw": "sha256:61195cafe75932921c2d0b36d0f75c8aebc38dd26db2202036f48051c780fd62",
        "canonical": "sha256:353d4580857ed95cc05434f770a7e69a7d62d55b27e214ce21fdf0c870444db7",
    },
    "tool_metadata/resolver_implementation_bindings_current.json": {
        "raw": "sha256:9affa0bfc1127965d0a9e6427100b412a867536386f55053738e3c87e6be1d88",
        "canonical": "sha256:7e914dd5527822eeaeb8bb819f221e285932eba18c9884939964de9d6abb4bc6",
    },
    "tool_authority/dependency_policy_current.json": {
        "raw": "sha256:6e3dec846f99df00574a41592fe611a873354ad50637f11e49a2957bf2a3b549",
        "canonical": "sha256:984cefc2a5fc18e9310111927029fdcd95225d93087d8db727d10b0ba0fc2986",
    },
    "tool_authority/policy_fingerprints_current.json": {
        "raw": "sha256:1917b9c1661ad3fb5ca60c85eebfe1488c839d63ac21a39f4c683359434c0994",
        "canonical": "sha256:e77b4aeddda7661fae590d2087ef75d3a5003fe0855ac076ac80c197e65eef76",
    },
    "tool_pipeline/provider_manifest_current.json": {
        "raw": "sha256:4e5b5971f6bde61652f06a35f1d45e9c663d6121f4448cb29f97925429eec98c",
        "canonical": "sha256:14f722ac74edb36f013c51202913b1dcc52bff256c48c552284ee7e52e5b25ac",
    },
}
CURRENT_GOLDEN_PRIVACY_CANARIES = (
    "yuqi.chen",
    "candidate secret",
    "sk-secret-value",
    "真实简历",
    "真实职位描述",
    "D:\\Users\\",
)


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


def test_current_goldens_have_independent_hash_pins_and_privacy_canaries() -> None:
    for relative_path, expected in CURRENT_GOLDEN_HASHES.items():
        value, raw = _read(ROOT / relative_path)
        raw_text = raw.decode("utf-8")
        assert raw == (_canonical(value) + "\n").encode("utf-8")
        assert _sha256(raw) == expected["raw"]
        assert _sha256(_canonical(value).encode("utf-8")) == expected["canonical"]
        for canary in CURRENT_GOLDEN_PRIVACY_CANARIES:
            assert canary not in raw_text


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
