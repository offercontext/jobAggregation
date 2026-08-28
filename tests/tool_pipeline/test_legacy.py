from __future__ import annotations

import inspect

from offerpilot.ai.tool_runtime.legacy import LegacyDeterministicCatalog
from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog
from offerpilot.ai.tool_specs.legacy import build_static_adapter_catalog


def test_legacy_catalog_is_exact_and_never_model_visible() -> None:
    legacy_names = frozenset(
        adapter.name for adapter in build_static_adapter_catalog().ordered_adapters
    )
    assert legacy_names == frozenset(
        {
            "save_application_jd_version",
            "create_application_submission_snapshot",
            "record_application_outcome",
        }
    )
    typed_catalog = build_model_tool_catalog()
    assert all(typed_catalog.resolve(name) is None for name in legacy_names)
    assert all(
        name not in {contract.name for contract in typed_catalog.provider_contracts()}
        for name in legacy_names
    )
    assert not hasattr(LegacyDeterministicCatalog, "resolve")


def test_server_loaded_legacy_resolution_is_proof_only() -> None:
    signature = inspect.signature(LegacyDeterministicCatalog.resolve_server_loaded)

    assert tuple(signature.parameters) == ("self", "proof")
    annotation = signature.parameters["proof"].annotation
    assert getattr(annotation, "__name__", annotation) == "LegacyRouteProof"
