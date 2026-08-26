from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from offerpilot.ai.tool_runtime import legacy as legacy_runtime
from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_specs import legacy as legacy_specs
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.pilot_runtime.compensation import prepare_compensation_handler_components


ROOT = Path(__file__).parents[2]
PRODUCTION_ROOT = ROOT / "src" / "offerpilot"
ORDERED_ADAPTERS = (
    "save_application_jd_version",
    "create_application_submission_snapshot",
    "record_application_outcome",
)


def _proof_module() -> Any:
    try:
        return importlib.import_module("offerpilot.ai.tool_runtime.legacy_proof")
    except ModuleNotFoundError:
        pytest.fail("Task 8 must provide the Legacy proof component module", pytrace=False)


def _symbol(name: str) -> Any:
    value = getattr(_proof_module(), name, None)
    assert value is not None, f"Task 8 must expose {name}"
    return value


def _route_module() -> Any:
    try:
        return importlib.import_module("offerpilot.pilot_runtime.legacy_route")
    except ModuleNotFoundError:
        pytest.fail("Task 8 must provide the Legacy verifier module", pytrace=False)


def _factory() -> Any:
    value = getattr(_route_module(), "build_unpublished_legacy_confirmation_components", None)
    assert callable(value), "Task 8 must expose the unpublished atomic factory"
    return value


def _boundary() -> Any:
    manifest = compile_tool_metadata_manifest(MODEL_TOOL_CATALOG.specs)
    bundle = ToolMetadataBundleV1(
        typed_catalog=MODEL_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=manifest.to_dict()["legacy_boundary"],  # type: ignore[arg-type]
        compensation=prepare_compensation_handler_components().metadata_projection(),
    )
    return bundle.legacy_boundary()


def _verifier() -> Any:
    try:
        route_module = _route_module()
    except ModuleNotFoundError:
        pytest.fail("Task 8 must provide the Legacy verifier module", pytrace=False)
    builder = getattr(route_module, "build_legacy_pending_identity_verifier_port", None)
    assert callable(builder), "Task 8 must expose the exact verifier Port builder"
    return builder(
        backend=SimpleNamespace(
            read_snapshot=lambda *_args, **_kwargs: {},
            locked_recheck=lambda *_args, **_kwargs: {},
            claim_cas=lambda *_args, **_kwargs: {},
        ),
        ledger_key=SimpleNamespace(key_id="00000000-0000-0000-0000-000000000001", secret=b"k" * 32),
    )


def _components() -> Any:
    factory = _factory()
    return factory(
        catalog=legacy_specs.build_static_legacy_adapter_catalog(),
        legacy_boundary=_boundary(),
        runtime_container_token=object(),
        pending_identity_verifier_port=_verifier(),
    )


def test_unpublished_factory_builds_one_identity_bound_complete_confirmation_graph() -> None:
    components = _components()

    assert type(components).__name__ == "LegacyConfirmationRouteComponents"
    assert type(components.preparation_registry).__name__ == "LegacyPreparationRegistry"
    assert type(components.proof_registry).__name__ == "LegacyRouteProofRegistry"
    assert type(components.proof_issuer).__name__ == "LegacyRouteProofIssuer"
    assert type(components.proof_consumer_port).__name__ == "LegacyRouteProofConsumerPort"
    assert (
        type(components.pending_identity_verifier_port).__name__
        == "LegacyPendingIdentityVerifierPort"
    )
    assert type(components.catalog).__name__ == "LegacyProofDeterministicCatalog"
    assert components.proof_issuer.preparation_registry is components.preparation_registry
    assert components.proof_issuer.proof_registry is components.proof_registry
    assert components.proof_consumer_port.proof_registry is components.proof_registry
    assert components.proof_consumer_port.preparation_registry is components.preparation_registry
    assert components.catalog.proof_consumer_port is components.proof_consumer_port
    assert (
        components.proof_issuer.pending_identity_verifier_port
        is components.pending_identity_verifier_port
    )
    assert not hasattr(components.catalog, "ordered_adapters")
    assert not hasattr(components.catalog, "resolve_name")
    assert (
        components.bundle_instance_token
        is components.catalog.bundle_instance_token
        is components.proof_issuer.bundle_instance_token
        is components.proof_consumer_port.bundle_instance_token
    )
    assert components.catalog_instance_token is components.catalog.catalog_instance_token
    assert components.catalog_instance_token is components.proof_registry.catalog_instance_token
    assert components.preparation_registry is not components.proof_registry
    assert not hasattr(components.proof_registry, "register")
    assert not hasattr(components.proof_consumer_port, "register")


def test_confirmation_components_are_sealed_and_do_not_publish_partial_topology() -> None:
    components = _components()
    with pytest.raises((AttributeError, TypeError)):
        components.catalog = object()  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        components.proof_registry = object()  # type: ignore[misc]

    factory = _factory()
    malformed = legacy_specs.build_static_legacy_adapter_catalog()
    object.__setattr__(malformed.ordered_adapters[0], "name", "corrupted")
    returned: list[object] = []
    with pytest.raises((TypeError, ValueError), match="integrity|Catalog|boundary|drift"):
        returned.append(
            factory(
                catalog=malformed,
                legacy_boundary=_boundary(),
                runtime_container_token=object(),
                pending_identity_verifier_port=_verifier(),
            )
        )
    assert returned == []


def test_factory_signature_requires_exact_catalog_boundary_container_and_verifier() -> None:
    factory = _factory()
    assert tuple(inspect.signature(factory).parameters) == (
        "catalog",
        "legacy_boundary",
        "runtime_container_token",
        "pending_identity_verifier_port",
    )
    valid = {
        "catalog": legacy_specs.build_static_legacy_adapter_catalog(),
        "legacy_boundary": _boundary(),
        "runtime_container_token": object(),
        "pending_identity_verifier_port": _verifier(),
    }
    for name, wrong in (
        ("catalog", object()),
        ("legacy_boundary", object()),
        ("runtime_container_token", SimpleNamespace()),
        ("pending_identity_verifier_port", object()),
    ):
        kwargs = {**valid, name: wrong}
        with pytest.raises((TypeError, ValueError), match=name.split("_")[0] + "|exact|opaque"):
            factory(**kwargs)


def test_confirmation_factory_has_no_production_caller_before_atomic_bundle_task() -> None:
    factory_name = "build_unpublished_legacy_confirmation_components"
    consumers: list[str] = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        if path == PRODUCTION_ROOT / "pilot_runtime" / "legacy_route.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == factory_name for alias in node.names
            ):
                consumers.append(str(path.relative_to(ROOT)))
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id == factory_name:
                    consumers.append(str(path.relative_to(ROOT)))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                if node.attr == factory_name:
                    consumers.append(str(path.relative_to(ROOT)))
    assert consumers == []


def test_legacy_proof_leaf_has_no_ledger_repository_orm_or_pilot_runtime_imports() -> None:
    path = PRODUCTION_ROOT / "ai" / "tool_runtime" / "legacy_proof.py"
    assert path.exists(), "Task 8 must add the proof Registry leaf"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_modules = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden_fragments = (
        "write_operations",
        "pilot_runtime",
        "repositories",
        "models",
        "sqlalchemy",
    )
    assert not any(
        fragment in module for module in imported_modules for fragment in forbidden_fragments
    )


def test_confirmation_route_reuses_the_single_legacy_argument_codec_and_preparer() -> None:
    path = PRODUCTION_ROOT / "pilot_runtime" / "legacy_route.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    json_load_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "json"
        and node.func.attr in {"load", "loads"}
    ]

    assert "_decode_legacy_object" not in function_names
    assert calls.count("_decode_legacy_arguments_object") >= 1
    assert calls.count("prepare_legacy_arguments") == 1
    assert json_load_calls == []


def test_proof_ports_do_not_accept_name_args_session_repository_or_ledger_key() -> None:
    components = _components()
    preparation_binding_type = _symbol("LegacyPreparationBinding")
    signatures = {
        "prepare": inspect.signature(components.proof_issuer.prepare_server_loaded),
        "issue": inspect.signature(components.proof_issuer.issue_after_claim),
        "resolve": inspect.signature(components.catalog.resolve_server_loaded),
        "consume": inspect.signature(components.proof_consumer_port.consume),
    }
    assert tuple(signatures["prepare"].parameters) == (
        "read_session",
        "lookup_identity",
        "confirmation_input",
    )
    assert tuple(signatures["issue"].parameters) == (
        "write_session",
        "issuance_lease",
        "claim_lease",
        "prepared_call",
    )
    assert tuple(signatures["resolve"].parameters) == ("proof",)
    assert tuple(signatures["consume"].parameters) == ("proof",)
    forbidden = {
        "tool_name",
        "name",
        "raw_args",
        "effective_args",
        "repository",
        "ledger_key",
        "session",
    }
    for label in ("resolve", "consume"):
        assert forbidden.isdisjoint(signatures[label].parameters)
    assert not hasattr(components.catalog, "ledger_key")
    assert not hasattr(components.catalog, "repository")
    assert not hasattr(components.proof_registry, "ledger_key")
    assert not hasattr(components.proof_registry, "effective_args")
    for forbidden_name in ("execute", "adapter", "repository", "session"):
        assert not hasattr(preparation_binding_type, forbidden_name)


def test_existing_server_loaded_catalog_signature_and_behavior_are_unchanged() -> None:
    calls: list[str] = []

    def execute(encoded_args: str) -> str:
        calls.append(encoded_args)
        return encoded_args

    adapters = tuple(
        legacy_runtime.LegacyDeterministicAdapter(
            name=name,
            editable_fields=(),
            describe=lambda encoded: encoded,
            validate=lambda _encoded: "",
            execute=execute,
        )
        for name in ORDERED_ADAPTERS
    )
    catalog = legacy_runtime.LegacyDeterministicCatalog(adapters)

    assert tuple(
        inspect.signature(
            legacy_runtime.LegacyDeterministicCatalog.resolve_server_loaded
        ).parameters
    ) == ("self", "pending")
    for adapter in adapters:
        pending = SimpleNamespace(tool_name=adapter.name)
        assert catalog.resolve_server_loaded(pending) is adapter
    assert catalog.resolve_server_loaded(SimpleNamespace(tool_name="unknown")) is None
    assert calls == []
    assert legacy_runtime.LEGACY_DETERMINISTIC_NAMES == frozenset(ORDERED_ADAPTERS)
    assert type(MappingProxyType({})) is MappingProxyType
