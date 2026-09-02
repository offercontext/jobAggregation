from __future__ import annotations

import ast
import hashlib
import inspect
import pickle
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from offerpilot.ai import client as ai_client
from offerpilot.ai.client import ConfiguredAIClient
from offerpilot.ai.tool_runtime.catalog import SegmentToolSpecHandle, ToolCatalog
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    BindingAudit,
    PreparedToolCall,
    ProviderToolContract,
    ToolExecutionRecord,
    ToolFailure,
    ToolSpec,
    materialize_provider_payloads,
)
from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    ToolMetadataBundleV1,
)
from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog
from offerpilot.ai.tool_specs.legacy import build_static_adapter_catalog
from offerpilot.ai.types import Message
from offerpilot.config import Config

from golden import canonical_json, load_golden
from tests.tool_metadata.factories import (
    compose_synthetic_bundle,
    read_metadata,
    synthetic_tool_spec,
    write_metadata,
)


_TEST_TOOL_CATALOG = build_model_tool_catalog()
_TEST_TOOL_NAMES = tuple(spec.name for spec in _TEST_TOOL_CATALOG.specs)
_TEST_LEGACY_NAMES = frozenset(
    adapter.name for adapter in build_static_adapter_catalog().ordered_adapters
)


def _test_spec_handle(spec: ToolSpec[Any, Any]) -> SegmentToolSpecHandle:
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    source = compose_synthetic_bundle()
    manifest = dict(cast(dict[str, object], source["manifest"]))
    manifest["typed_tools"] = (spec.name,)
    bundle = ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=manifest,
        legacy_boundary=cast(dict[str, object], source["legacy_boundary"]),
        compensation=cast(dict[str, object], source["compensation"]),
    )
    lease = bundle.open_segment_lease()
    handle = lease.resolve(spec.name)
    assert handle is not None
    assert lease.require_spec(handle) is spec
    return handle


def _contract(name: str, schema: dict[str, Any] | None = None) -> ProviderToolContract:
    parameters = schema or {"properties": {}, "type": "object"}
    payload = {
        "type": "function",
        "function": {
            "description": f"{name} description",
            "name": name,
            "parameters": parameters,
            "strict": False,
        },
    }
    return ProviderToolContract(
        payload=payload,
        name=name,
        description=f"{name} description",
        parameters=parameters,
    )


def _spec(
    name: str,
    *,
    kind: str = "read",
    schema: dict[str, Any] | None = None,
) -> ToolSpec[dict[str, Any], dict[str, Any]]:
    metadata = write_metadata() if kind == "write" else read_metadata()
    metadata = replace(metadata, editable_fields=())
    return replace(
        synthetic_tool_spec(name, metadata=metadata),
        contract=_contract(name, schema),
    )


def test_catalog_preserves_order_full_provider_envelopes_and_write_names() -> None:
    read = _spec("read_one")
    write = _spec("write_one", kind="write")
    catalog = ToolCatalog([read, write], expected_names=("read_one", "write_one"))

    assert catalog.resolve("read_one") is read
    assert catalog.resolve("missing") is None
    assert catalog.provider_contracts() == (read.contract, write.contract)
    assert catalog.provider_contracts()[0].payload["function"]["strict"] is False
    assert catalog.write_names() == frozenset({"write_one"})
    assert catalog.validator_for("read_one").schema == read.contract.parameters


@pytest.mark.parametrize(
    ("specs", "expected_names"),
    (
        ((_spec("one"),), ("other",)),
        ((_spec("one"), _spec("one")), ("one", "one")),
        ((_spec("one"), _spec("two")), ("two", "one")),
    ),
)
def test_catalog_rejects_missing_duplicate_or_reordered_names(
    specs: tuple[ToolSpec[Any, Any], ...],
    expected_names: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="tool catalog names/order mismatch"):
        ToolCatalog(specs, expected_names=expected_names)


def test_catalog_rejects_invalid_schema_during_construction() -> None:
    spec = _spec("broken", schema={"type": "not-a-type", "properties": {}})

    with pytest.raises(ValueError, match="invalid_tool_schema"):
        ToolCatalog([spec], expected_names=("broken",))


def test_runtime_catalog_has_no_reverse_dependency_on_tool_specs() -> None:
    source_path = (
        Path(__file__).parents[2] / "src" / "offerpilot" / "ai" / "tool_runtime" / "catalog.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )

    assert not any("tool_specs" in module for module in imported_modules)


def test_transient_runtime_values_reject_pickle_and_hide_sensitive_fields() -> None:
    spec = _spec("read_one")
    failure = ToolFailure(
        category="internal_error",
        code="executor_exception",
        compatibility_detail="private exception text",
    )
    prepared = PreparedToolCall(
        arguments={"private": "sensitive-argument-value"},
        arguments_digest="sha256:" + "a" * 64,
        binding=BindingAudit(status="unavailable", target_count=0),
        contract_fingerprint="sha256:" + "b" * 64,
        spec=spec,
        spec_handle=_test_spec_handle(spec),
        tool_call_id="call-1",
        typed_args={"private": "sensitive-argument-value"},
    )
    record = ToolExecutionRecord(
        execution_started=False,
        outcome=failure,
        prepared=prepared,
    )

    for value in (failure, prepared, record):
        with pytest.raises(TypeError, match="transient tool runtime value"):
            pickle.dumps(value)
        with pytest.raises(TypeError, match="transient tool runtime value"):
            value.__getstate__()

    rendered = repr((failure, prepared, record))
    assert "private exception text" not in rendered
    assert "sensitive-argument-value" not in rendered


def test_prepared_tool_call_requires_a_typed_segment_spec_handle() -> None:
    parameter = inspect.signature(PreparedToolCall).parameters["spec_handle"]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation == "SegmentToolSpecHandleLike"
    assert PreparedToolCall.__dataclass_params__.frozen is True


def test_model_catalog_is_exact_provider_golden_in_exact_order() -> None:
    manifest = load_golden("provider_manifest_current.json")
    contracts = _TEST_TOOL_CATALOG.provider_contracts()

    assert len(_TEST_TOOL_NAMES) == 26
    assert len(set(_TEST_TOOL_NAMES)) == 26
    assert tuple(contract.name for contract in contracts) == _TEST_TOOL_NAMES
    payloads = materialize_provider_payloads(contracts)
    assert canonical_json(payloads) == canonical_json(manifest["tools"])
    actual_fingerprints = {
        contract.name: "sha256:"
        + hashlib.sha256(
            canonical_json(payload["function"]["parameters"]).encode("utf-8")
        ).hexdigest()
        for contract, payload in zip(contracts, payloads, strict=True)
    }
    assert actual_fingerprints == manifest["schema_fingerprints"]


def test_final_provider_adapter_receives_exact_golden_envelopes(monkeypatch) -> None:
    manifest = load_golden("provider_manifest_current.json")
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr(ai_client, "completion", fake_completion)
    ConfiguredAIClient(Config(api_key="synthetic-key")).complete(
        [Message(role="user", content="synthetic")],
        list(_TEST_TOOL_CATALOG.provider_contracts()),
    )

    assert canonical_json(captured["tools"]) == canonical_json(manifest["tools"])


def test_complete_tool_classification_is_exactly_twenty_six_typed_plus_three_legacy() -> None:
    typed = frozenset(_TEST_TOOL_NAMES)

    assert len(typed) == 26
    assert len(_TEST_LEGACY_NAMES) == 3
    assert typed.isdisjoint(_TEST_LEGACY_NAMES)
    assert len(typed | _TEST_LEGACY_NAMES) == 29


def test_catalog_rejects_unknown_capability_and_resolver_metadata() -> None:
    spec = _spec("read_one")
    original = spec.metadata.required_capabilities
    object.__setattr__(spec.metadata, "required_capabilities", (cast(Any, "future.read"),))
    try:
        with pytest.raises((TypeError, ValueError), match="capabilit|metadata"):
            ToolCatalog(
                [spec],
                expected_names=("read_one",),
                authority_manifest={"schema_version": 1, "tools": []},
            )
    finally:
        object.__setattr__(spec.metadata, "required_capabilities", original)


def test_binding_contract_and_resolver_descriptor_have_closed_fields() -> None:
    contract = BindingContract(kind="enforce_if_bound", entity_kind="application")
    resolver = BindingResolverDescriptorV1(
        resolver_id="application_identity_arg",
        entity_kind="application",
        arg_path="id",
        presence="required",
        identity_type="positive_int64",
    )
    assert contract.entity_kind == "application"
    assert resolver.arg_path == "id"
