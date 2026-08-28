from __future__ import annotations

import inspect

from offerpilot.ai.tool_runtime.legacy import (
    LEGACY_DETERMINISTIC_NAMES,
    LegacyDeterministicCatalog,
)
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG


def test_legacy_catalog_is_exact_and_never_model_visible() -> None:
    assert LEGACY_DETERMINISTIC_NAMES == frozenset(
        {
            "save_application_jd_version",
            "create_application_submission_snapshot",
            "record_application_outcome",
        }
    )
    assert all(MODEL_TOOL_CATALOG.resolve(name) is None for name in LEGACY_DETERMINISTIC_NAMES)
    assert all(
        name not in {contract.name for contract in MODEL_TOOL_CATALOG.provider_contracts()}
        for name in LEGACY_DETERMINISTIC_NAMES
    )
    assert not hasattr(LegacyDeterministicCatalog, "resolve")


def test_server_loaded_legacy_resolution_is_proof_only() -> None:
    signature = inspect.signature(LegacyDeterministicCatalog.resolve_server_loaded)

    assert tuple(signature.parameters) == ("self", "proof")
    annotation = signature.parameters["proof"].annotation
    assert getattr(annotation, "__name__", annotation) == "LegacyRouteProof"
