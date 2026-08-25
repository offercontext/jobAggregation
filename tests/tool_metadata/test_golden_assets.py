from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from .golden import canonical_bytes, load_asset, sha256


BASELINE = "0c10e05e256eb757d5f89a8b009dcea193f2fc78"
FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
REPOSITORY_ROOT = FIXTURE_ROOT.parents[1]
METADATA_FIXTURES = FIXTURE_ROOT / "tool_metadata"
ASSET_NAMES = (
    "golden_index_v1.json",
    "tool_metadata_manifest_v1.json",
    "tool_selection_matrix_0c10e05.json",
    "tool_operation_matrix_0c10e05.json",
    "resolver_implementation_bindings_0c10e05.json",
)
INDEXED_ASSETS = ASSET_NAMES[1:]
INDEPENDENT_GOLDENS = (
    "tool_pipeline/provider_manifest_30c944f.json",
    "tool_authority/authority_manifest_v1.json",
    "tool_authority/dependency_policy_v1.json",
)
PRODUCTION_MANIFEST_KEYS = {
    "schema_version",
    "metadata_version",
    "catalog_profile",
    "typed_tools",
    "discovery_policy",
    "legacy_boundary",
    "compensation_operation_order",
}
TYPED_ENTRY_KEYS = {
    "ordinal",
    "provider_name",
    "provider_contract_fingerprint",
    "domains",
    "dependencies",
    "provider_visibility",
    "required_capabilities",
    "binding",
    "confirmation_policy",
    "editable_fields",
    "operation",
}
WRITE_OPERATION_KEYS = {
    "kind",
    "adapter_kind",
    "result_contract",
    "result_bytes",
    "visible_bytes",
    "transport_bytes",
    "undo_bytes",
    "undo_policy",
    "undo_payload_kind",
    "compensation_kind",
    "undo_contract_version",
    "undo_builder_id",
    "undo_seed_phase",
}
LEGACY_NAMES = (
    "save_application_jd_version",
    "create_application_submission_snapshot",
    "record_application_outcome",
)
COMPENSATION_KINDS = (
    "undo:update_application_status",
    "undo:create_application",
    "undo:create_application_event",
    "undo:add_note",
)
REQUIRED_UNDO = {
    "create_application": (
        "delete_application",
        "undo:create_application",
        "create_application_delete_v1",
        "none",
    ),
    "update_application_status": (
        "update_application_status",
        "undo:update_application_status",
        "update_application_status_restore_v1",
        "before_execute",
    ),
    "create_application_event": (
        "delete_application_event",
        "undo:create_application_event",
        "create_application_event_delete_v1",
        "none",
    ),
    "add_note": ("delete_note", "undo:add_note", "add_note_delete_v1", "none"),
}
FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "auth_token",
        "confirmation_secret",
        "exception",
        "private_key",
        "prompt",
        "secret",
        "stack_trace",
        "timestamp",
        "traceback",
    }
)
REAL_USER_CANARIES = (
    "yuqi.chen",
    "candidate secret",
    "sk-secret-value",
    "真实简历",
    "真实职位描述",
)
WINDOWS_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]")
OBJECT_ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _read_fixture(relative: str) -> tuple[Any, bytes]:
    raw = (FIXTURE_ROOT / relative).read_bytes()
    return json.loads(raw.decode("utf-8")), raw


def _read_committed_blob(relative: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", f":{relative}"],
        cwd=REPOSITORY_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return completed.stdout


def _portable_sha(value: Any) -> str:
    raw = canonical_bytes(value)
    return sha256(raw[:-1])


def _surface_sha(values: Sequence[Any]) -> str:
    raw = canonical_bytes(values)
    return hashlib.sha256(raw[:-1]).hexdigest()


def _walk_private(value: Any) -> None:
    if isinstance(value, Mapping):
        lowered = {str(key).lower() for key in value}
        assert not FORBIDDEN_KEYS & lowered
        assert all(type(key) is str for key in value)
        for nested in value.values():
            _walk_private(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk_private(nested)


def _provider_names(provider: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(item["function"]["name"] for item in provider["tools"])


def test_required_assets_exist_before_the_loader_can_read_them() -> None:
    assert tuple(path.name for path in sorted(METADATA_FIXTURES.glob("*.json"))) == tuple(
        sorted(ASSET_NAMES)
    )
    for name in ASSET_NAMES:
        assert load_asset(name) is not None


def test_loader_contract_is_canonical_bounded_and_read_only() -> None:
    from . import golden

    assert golden.__all__ == ("load_asset", "canonical_bytes", "sha256")
    assert canonical_bytes({"中": [2, 1], "a": "值"}) == (
        b'{"a":"\xe5\x80\xbc","\xe4\xb8\xad":[2,1]}\n'
    )
    assert sha256(b"abc") == (
        "sha256:ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad"
    )
    with pytest.raises(ValueError):
        canonical_bytes(float("nan"))
    with pytest.raises(TypeError):
        sha256(bytearray(b"abc"))  # type: ignore[arg-type]
    for invalid in ("", "../asset.json", "sub/asset.json", r"sub\asset.json", "asset.txt"):
        with pytest.raises(ValueError):
            load_asset(invalid)


def test_tool_metadata_test_tree_has_no_writer_acceptance_or_update_path() -> None:
    root = Path(__file__).parent
    function_names: set[str] = set()
    forbidden_methods = {
        "rename",
        "truncate",
        "unlink",
        "write",
        "write_bytes",
        "write_text",
        "writelines",
    }
    for source_path in root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        if source_path.name == "golden.py":
            function_names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = node.func
                if isinstance(called, ast.Attribute):
                    assert called.attr not in forbidden_methods
                    assert called.attr != "getenv"
                    if called.attr == "open":
                        modes = [
                            arg.value
                            for arg in node.args[1:2]
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        ]
                        assert not any(set(mode) & set("wax+") for mode in modes)
                    if called.attr == "add_argument":
                        flags = {
                            arg.value
                            for arg in node.args
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        }
                        assert not flags & {"--accept", "--accept-new", "--update"}
                elif isinstance(called, ast.Name) and called.id == "open":
                    modes = [
                        arg.value
                        for arg in node.args[1:2]
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    ]
                    assert not any(set(mode) & set("wax+") for mode in modes)
            if isinstance(node, ast.Attribute):
                assert node.attr != "environ"
    assert function_names == {"load_asset", "canonical_bytes", "sha256"}


@pytest.mark.parametrize("asset_name", ASSET_NAMES)
def test_asset_is_canonical_private_and_pinned(asset_name: str) -> None:
    value = load_asset(asset_name)
    raw = _read_committed_blob(f"tests/fixtures/tool_metadata/{asset_name}")
    text = raw.decode("utf-8")
    assert raw == canonical_bytes(value)
    assert not WINDOWS_ABSOLUTE_PATH.search(text)
    assert not OBJECT_ADDRESS.search(text)
    assert "SQLite format 3" not in text
    assert "Traceback (most recent call last)" not in text
    for canary in REAL_USER_CANARIES:
        assert canary not in text
    _walk_private(value)


def test_index_pins_child_assets_and_existing_independent_goldens() -> None:
    index = load_asset("golden_index_v1.json")
    assert set(index) == {
        "schema_version",
        "source_baseline",
        "assets",
        "independent_goldens",
    }
    assert index["schema_version"] == 1
    assert index["source_baseline"] == BASELINE
    assert tuple(item["name"] for item in index["assets"]) == INDEXED_ASSETS
    assert tuple(item["path"] for item in index["independent_goldens"]) == (
        INDEPENDENT_GOLDENS
    )
    for item in index["assets"]:
        assert set(item) == {"name", "raw_sha256", "canonical_sha256"}
        value = load_asset(item["name"])
        raw = _read_committed_blob(f"tests/fixtures/tool_metadata/{item['name']}")
        assert item["raw_sha256"] == sha256(raw)
        assert item["canonical_sha256"] == sha256(canonical_bytes(value))
        assert DIGEST.fullmatch(item["raw_sha256"])
    for item in index["independent_goldens"]:
        assert set(item) == {"path", "raw_sha256", "canonical_sha256"}
        value, _ = _read_fixture(item["path"])
        raw = _read_committed_blob(f"tests/fixtures/{item['path']}")
        assert item["raw_sha256"] == sha256(raw)
        assert item["canonical_sha256"] == sha256(canonical_bytes(value))


def test_metadata_manifest_matches_the_three_independent_goldens() -> None:
    manifest = load_asset("tool_metadata_manifest_v1.json")
    provider, _ = _read_fixture(INDEPENDENT_GOLDENS[0])
    authority, _ = _read_fixture(INDEPENDENT_GOLDENS[1])
    dependency, _ = _read_fixture(INDEPENDENT_GOLDENS[2])
    assert set(manifest) == PRODUCTION_MANIFEST_KEYS
    assert manifest["schema_version"] == 1
    assert manifest["metadata_version"] == "tool-surface-metadata-v1"
    assert manifest["catalog_profile"] == "agent_typed_v1"
    tools = manifest["typed_tools"]
    assert len(tools) == 25
    assert tuple(item["provider_name"] for item in tools) == _provider_names(provider)
    assert tuple(item["ordinal"] for item in tools) == tuple(range(1, 26))
    assert len({item["provider_name"] for item in tools}) == 25
    provider_by_name = {
        item["function"]["name"]: item for item in provider["tools"]
    }
    authority_projection = []
    for item in tools:
        assert set(item) == TYPED_ENTRY_KEYS
        assert item["provider_contract_fingerprint"] == _portable_sha(
            provider_by_name[item["provider_name"]]
        )
        assert item["provider_visibility"] == "model_eligible"
        assert item["domains"] == sorted(item["domains"])
        assert item["domains"] and len(item["domains"]) == len(set(item["domains"]))
        assert item["dependencies"] == sorted(item["dependencies"])
        assert item["binding"].keys() == {"contract", "resolver_descriptors"}
        assert all(
            set(field) == {"field", "value_type", "options", "clearable", "clear_value"}
            for field in item["editable_fields"]
        )
        operation = item["operation"]
        if operation["kind"] == "read":
            assert set(operation) == {"kind"}
        else:
            assert set(operation) == WRITE_OPERATION_KEYS
            assert operation["kind"] == "transactional_write"
        authority_projection.append(
            {
                "ordinal": item["ordinal"],
                "name": item["provider_name"],
                "kind": "write" if operation["kind"] == "transactional_write" else "read",
                "confirmation_policy": item["confirmation_policy"],
                "required_capabilities": item["required_capabilities"],
                "binding": item["binding"]["contract"],
                "resolvers": item["binding"]["resolver_descriptors"],
            }
        )
    assert {"schema_version": 1, "tools": authority_projection} == authority
    dependency_projection = {
        item["provider_name"]: item["dependencies"] for item in tools
    }
    assert dependency_projection == dependency["dependencies"]
    assert dependency["catalog_names"] == list(_provider_names(provider))
    assert dependency["coverage"] == 25
    dependency_manifest = {
        "dependency_policy_version": dependency["dependency_policy_version"],
        "catalog_names": dependency["catalog_names"],
        "dependencies": dependency["dependencies"],
    }
    assert dependency["canonical_sha256"] == _portable_sha(dependency_manifest)


def test_manifest_closes_discovery_legacy_compensation_and_operation_invariants() -> None:
    manifest = load_asset("tool_metadata_manifest_v1.json")
    tools = manifest["typed_tools"]
    discovery = manifest["discovery_policy"]
    assert set(discovery) == {
        "selector_version",
        "discovery_policy_version",
        "page_domains",
        "attachment_domains",
        "lexical_rules",
        "no_signal_behavior",
        "invalid_input_behavior",
    }
    assert tuple(item["page_kind"] for item in discovery["page_domains"]) == (
        "workspace",
        "applications",
        "application",
        "calendar",
        "notes",
        "offers",
        "resumes",
    )
    assert tuple(item["attachment_kind"] for item in discovery["attachment_domains"]) == (
        "resume",
        "job_description",
        "image",
        "document",
    )
    assert tuple(item["domain"] for item in discovery["lexical_rules"]) == (
        "applications",
        "events",
        "notes",
        "offers",
        "resumes",
        "jd",
    )
    assert discovery["no_signal_behavior"] == "full_typed_catalog"
    assert discovery["invalid_input_behavior"] == "fail_closed"
    writes = [item for item in tools if item["operation"]["kind"] == "transactional_write"]
    assert len(writes) == 12
    required = [item for item in writes if item["operation"]["undo_policy"] == "required"]
    assert len(required) == 4
    for item in writes:
        operation = item["operation"]
        assert operation["adapter_kind"] == "typed"
        assert operation["result_contract"] == "typed_json_v1"
        assert operation["result_bytes"] == 512 * 1024
        assert operation["visible_bytes"] == 256 * 1024
        assert operation["transport_bytes"] == 128 * 1024
        assert operation["undo_bytes"] == 64 * 1024
        if item["provider_name"] in REQUIRED_UNDO:
            payload, compensation, builder, phase = REQUIRED_UNDO[item["provider_name"]]
            assert (
                operation["undo_payload_kind"],
                operation["compensation_kind"],
                operation["undo_builder_id"],
                operation["undo_seed_phase"],
            ) == (payload, compensation, builder, phase)
            assert operation["undo_contract_version"] == "write-undo-payload-v1"
        else:
            assert operation["undo_policy"] == "none"
            assert all(
                operation[key] is None
                for key in (
                    "undo_payload_kind",
                    "compensation_kind",
                    "undo_contract_version",
                    "undo_builder_id",
                    "undo_seed_phase",
                )
            )
    legacy = manifest["legacy_boundary"]
    assert set(legacy) == {
        "boundary_version",
        "provider_visibility",
        "adapter_kind",
        "ordered_names",
        "chained_policies",
        "initial_route_bindings",
    }
    assert tuple(legacy["ordered_names"]) == LEGACY_NAMES
    assert legacy["provider_visibility"] == "forbidden"
    assert legacy["adapter_kind"] == "legacy_deterministic"
    assert legacy["chained_policies"] == ["same_adapter_only", "forbidden", "forbidden"]
    assert tuple(manifest["compensation_operation_order"]) == COMPENSATION_KINDS
    assert not set(legacy["ordered_names"]) & {item["provider_name"] for item in tools}


def _reference_selection(
    manifest: Mapping[str, Any], provider: Mapping[str, Any], signals: Mapping[str, Any]
) -> dict[str, Any]:
    policy = manifest["discovery_policy"]
    page_domains = {item["page_kind"]: item["domains"] for item in policy["page_domains"]}
    attachment_domains = {
        item["attachment_kind"]: item["domains"] for item in policy["attachment_domains"]
    }
    lexical_rules = {item["domain"]: item["terms"] for item in policy["lexical_rules"]}
    domains = set(signals["trusted_domains"])
    domains.update(page_domains[signals["page_kind"]])
    for attachment in signals["attachment_kinds"]:
        domains.update(attachment_domains[attachment])
    normalized = signals["current_request"].casefold()[:16_384]
    for domain, terms in lexical_rules.items():
        if any(term in normalized for term in terms):
            domains.add(domain)
    tool_by_name = {item["provider_name"]: item for item in manifest["typed_tools"]}
    ordered_names = tuple(tool_by_name)
    if not domains:
        selected = set(ordered_names)
    else:
        selected = {
            name
            for name, item in tool_by_name.items()
            if domains.intersection(item["domains"])
        }
        pending = list(selected)
        while pending:
            name = pending.pop()
            for dependency in tool_by_name[name]["dependencies"]:
                if dependency not in selected:
                    selected.add(dependency)
                    pending.append(dependency)
    closure = tuple(name for name in ordered_names if name in selected)
    provider_by_name = {
        item["function"]["name"]: item for item in provider["tools"]
    }
    envelopes = [provider_by_name[name] for name in closure]
    return {
        "domains": sorted(domains),
        "fallback_all": not domains,
        "fallback_reason": "no_trusted_signal" if not domains else None,
        "ordered_names": list(closure),
        "dependency_closure": list(closure),
        "surface_fingerprint": _surface_sha(envelopes),
    }


def test_selection_matrix_covers_every_declared_signal_and_matches_reference() -> None:
    matrix = load_asset("tool_selection_matrix_0c10e05.json")
    manifest = load_asset("tool_metadata_manifest_v1.json")
    provider, _ = _read_fixture(INDEPENDENT_GOLDENS[0])
    assert set(matrix) == {"schema_version", "source_baseline", "cases"}
    assert matrix["schema_version"] == 1
    assert matrix["source_baseline"] == BASELINE
    cases = matrix["cases"]
    assert len(cases) == 48
    assert len({case["case_id"] for case in cases}) == 48
    for case in cases:
        assert set(case) == {
            "case_id",
            "signals",
            "domains",
            "fallback_all",
            "fallback_reason",
            "ordered_names",
            "dependency_closure",
            "surface_fingerprint",
        }
        assert case["signals"].keys() == {
            "page_kind",
            "attachment_kinds",
            "current_request",
            "trusted_domains",
            "version",
        }
        assert case["signals"]["version"] == "tool-surface-selector-v1"
        expected = _reference_selection(manifest, provider, case["signals"])
        assert {key: case[key] for key in expected} == expected
    discovery = manifest["discovery_policy"]
    assert {case["signals"]["page_kind"] for case in cases if case["case_id"].startswith("page:")} == {
        item["page_kind"] for item in discovery["page_domains"]
    }
    assert {
        case["signals"]["attachment_kinds"][0]
        for case in cases
        if case["case_id"].startswith("attachment:")
    } == {item["attachment_kind"] for item in discovery["attachment_domains"]}
    lexical_cases = [case for case in cases if case["case_id"].startswith("lexical:")]
    assert len(lexical_cases) == sum(
        len(item["terms"]) for item in discovery["lexical_rules"]
    )


def test_operation_matrix_is_the_exact_25_3_4_4_projection() -> None:
    matrix = load_asset("tool_operation_matrix_0c10e05.json")
    manifest = load_asset("tool_metadata_manifest_v1.json")
    assert set(matrix) == {
        "schema_version",
        "source_baseline",
        "typed_operations",
        "legacy_operations",
        "required_undo_bindings",
        "compensation_operations",
    }
    assert matrix["schema_version"] == 1
    assert matrix["source_baseline"] == BASELINE
    assert len(matrix["typed_operations"]) == 25
    assert len(matrix["legacy_operations"]) == 3
    assert len(matrix["required_undo_bindings"]) == 4
    assert len(matrix["compensation_operations"]) == 4
    assert matrix["typed_operations"] == [
        {
            "ordinal": item["ordinal"],
            "name": item["provider_name"],
            "operation_kind": item["operation"]["kind"],
            "authority_kind": (
                "write" if item["operation"]["kind"] == "transactional_write" else "read"
            ),
            "confirmation_policy": item["confirmation_policy"],
            "undo_policy": item["operation"].get("undo_policy"),
        }
        for item in manifest["typed_tools"]
    ]
    assert tuple(item["name"] for item in matrix["legacy_operations"]) == LEGACY_NAMES
    assert tuple(
        item["compensation_kind"] for item in matrix["compensation_operations"]
    ) == COMPENSATION_KINDS
    required_names = {item["primary_tool"] for item in matrix["required_undo_bindings"]}
    assert required_names == set(REQUIRED_UNDO)


def test_resolver_implementation_asset_is_tool_local_and_matches_manifest_authority() -> None:
    asset = load_asset("resolver_implementation_bindings_0c10e05.json")
    manifest = load_asset("tool_metadata_manifest_v1.json")
    assert set(asset) == {"schema_version", "source_baseline", "bindings"}
    assert asset["schema_version"] == 1
    assert asset["source_baseline"] == BASELINE
    bindings = asset["bindings"]
    assert len(bindings) == 22
    assert len({(item["tool"], item["ordinal"]) for item in bindings}) == 22
    assert len({item["implementation_id"] for item in bindings}) == 22
    by_tool: dict[str, list[Mapping[str, Any]]] = {}
    for item in bindings:
        assert set(item) == {
            "tool",
            "ordinal",
            "descriptor",
            "implementation_id",
            "qualified_callable",
        }
        assert item["implementation_id"] == (
            f"{item['tool']}_{item['descriptor']['resolver_id']}_v1"
        )
        assert "<locals>" not in item["qualified_callable"]
        assert "<lambda>" not in item["qualified_callable"]
        assert not OBJECT_ADDRESS.search(item["qualified_callable"])
        by_tool.setdefault(item["tool"], []).append(item)
    for tool in manifest["typed_tools"]:
        expected = tool["binding"]["resolver_descriptors"]
        actual = by_tool.get(tool["provider_name"], [])
        assert [item["ordinal"] for item in actual] == list(range(1, len(actual) + 1))
        assert [item["descriptor"] for item in actual] == expected


def test_production_does_not_import_or_read_review_only_assets() -> None:
    source_root = Path(__file__).parents[2] / "src"
    asset_names = set(ASSET_NAMES)
    for source_path in source_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("tests") for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or not node.module.startswith("tests")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                normalized = node.value.translate(str.maketrans({"\\": "/"}))
                assert "tests/fixtures/tool_metadata" not in normalized
                assert normalized not in asset_names
