from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import offerpilot.context_projector.authority_surface as authority_surface_module
import offerpilot.context_projector.selector as selector_module
from offerpilot.ai.tool_authority.policy import (
    CAPABILITY_POLICY_VERSION,
    DEPENDENCY_POLICY_VERSION,
    PROFILE_ID,
)
from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_runtime.policy_types import ToolDomain
from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog
from offerpilot.context_projector.authority_surface import (
    AuthoritySurfaceView,
    intersect_authority_surface,
)
from offerpilot.context_projector.contracts import ProjectionError, canonical_json, sha256_hex
from offerpilot.context_projector.selector import (
    ToolSelectionDiagnostic,
    ToolSelectionDiagnosticKind,
    ToolSelectionSignals,
    select_tools,
)
from offerpilot.pilot_runtime.compensation import prepare_compensation_handler_components


_TEST_TOOL_CATALOG = build_model_tool_catalog()


def _bundle() -> ToolMetadataBundleV1:
    manifest = compile_tool_metadata_manifest(_TEST_TOOL_CATALOG.specs)
    projection = manifest.to_dict()
    compensation = prepare_compensation_handler_components()
    return ToolMetadataBundleV1(
        typed_catalog=_TEST_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=cast(dict[str, object], projection["legacy_boundary"]),
        compensation=compensation.metadata_projection(),
    )


def _reason_value(reason: object) -> object:
    return getattr(reason, "value", reason)


def _select(
    bundle: ToolMetadataBundleV1,
    signals: ToolSelectionSignals,
) -> Any:
    return select_tools(bundle.discovery_view(), bundle.authority_view(), signals)


def test_selector_requires_discovery_and_authority_views_from_one_bundle() -> None:
    first = _bundle()
    second = _bundle()
    discovery = first.discovery_view()
    authority = first.authority_view()

    assert discovery.bundle_instance_token is authority.bundle_instance_token
    result = select_tools(discovery, authority, ToolSelectionSignals(page_kind="workspace"))
    result_type = getattr(selector_module, "ToolSelectionResult", None)
    assert result_type is not None
    assert type(result) is result_type

    with pytest.raises(ProjectionError, match="[Bb]undle"):
        select_tools(discovery, second.authority_view(), ToolSelectionSignals())


def test_no_trusted_signal_returns_the_complete_ordered_typed_catalog() -> None:
    bundle = _bundle()
    result = _select(bundle, ToolSelectionSignals(page_kind="workspace"))
    expected_names = tuple(spec.name for spec in _TEST_TOOL_CATALOG.specs)

    assert result.provider_contracts == _TEST_TOOL_CATALOG.provider_contracts()
    assert result.selected_names == expected_names
    assert result.selected_domains == ()
    assert result.dependency_closure == expected_names
    assert result.full_catalog_fallback is True
    assert _reason_value(result.fallback_reason) == "no_trusted_signal"
    assert type(result.diagnostics) is tuple


def test_declared_ambiguity_returns_the_complete_catalog_with_a_distinct_reason() -> None:
    bundle = _bundle()
    signals = ToolSelectionSignals(
        page_kind="offers",
        trusted_domains=("offers",),
        declared_ambiguous_input=True,
    )

    result = _select(bundle, signals)

    assert result.selected_names == tuple(spec.name for spec in _TEST_TOOL_CATALOG.specs)
    assert result.provider_contracts == _TEST_TOOL_CATALOG.provider_contracts()
    assert result.full_catalog_fallback is True
    assert _reason_value(result.fallback_reason) == "declared_ambiguous_input"


@pytest.mark.parametrize(
    "signals",
    [
        ToolSelectionSignals(page_kind="unknown"),
        ToolSelectionSignals(attachment_kinds=("unknown",)),
        ToolSelectionSignals(trusted_domains=("unknown",)),
        ToolSelectionSignals(version="unknown"),
    ],
)
def test_unknown_or_invalid_signals_fail_before_provider_materialization(
    monkeypatch: pytest.MonkeyPatch,
    signals: ToolSelectionSignals,
) -> None:
    calls = 0

    def forbidden_materialization(contracts: object) -> list[dict[str, object]]:
        nonlocal calls
        del contracts
        calls += 1
        raise AssertionError("invalid signals reached the Provider boundary")

    monkeypatch.setattr(
        selector_module,
        "materialize_provider_payloads",
        forbidden_materialization,
    )
    bundle = _bundle()

    with pytest.raises(ProjectionError):
        _select(bundle, signals)
    assert calls == 0


def test_dependency_closure_uses_catalog_order_and_excludes_legacy_adapters() -> None:
    bundle = _bundle()
    result = _select(
        bundle,
        ToolSelectionSignals(page_kind="workspace", trusted_domains=("offers",)),
    )
    expected = (
        "list_offers",
        "get_offer",
        "compare_offers",
        "update_offer",
        "save_offer_assessment",
    )
    legacy_names = {binding.name for binding in bundle.legacy_boundary().ordered_adapter_bindings}

    assert result.selected_names == expected
    assert result.dependency_closure == expected
    assert result.selected_domains == (ToolDomain.OFFERS,)
    assert not legacy_names.intersection(result.selected_names)
    assert not legacy_names.intersection(result.dependency_closure)
    assert result.full_catalog_fallback is False
    assert result.fallback_reason is None


def test_provider_envelopes_are_materialized_and_fingerprinted_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    original: Callable[..., list[dict[str, object]]] = selector_module.materialize_provider_payloads
    original_sha256: Callable[[bytes], str] = selector_module.sha256_hex
    calls: list[tuple[object, ...]] = []
    fingerprint_inputs: list[bytes] = []

    def recording_materialization(contracts: object) -> list[dict[str, object]]:
        ordered = tuple(cast(Any, contracts))
        calls.append(ordered)
        return original(ordered)

    def recording_sha256(payload: bytes) -> str:
        fingerprint_inputs.append(payload)
        return original_sha256(payload)

    monkeypatch.setattr(
        selector_module,
        "materialize_provider_payloads",
        recording_materialization,
    )
    monkeypatch.setattr(selector_module, "sha256_hex", recording_sha256)

    result = _select(
        bundle,
        ToolSelectionSignals(page_kind="workspace", trusted_domains=("offers",)),
    )
    expected_payloads = original(result.provider_contracts)
    expected_bytes = canonical_json(expected_payloads)

    assert calls == [result.provider_contracts]
    assert fingerprint_inputs == [expected_bytes]
    assert result.provider_envelope_fingerprint == sha256_hex(expected_bytes)
    assert "secret-probe" not in repr(result.diagnostics)


def test_unknown_discovery_policy_version_fails_closed() -> None:
    manifest = compile_tool_metadata_manifest(_TEST_TOOL_CATALOG.specs)
    projection = manifest.to_dict()
    policy = cast(dict[str, object], projection["discovery_policy"])
    policy["discovery_policy_version"] = "future-discovery-policy-v999"
    bundle = ToolMetadataBundleV1(
        typed_catalog=_TEST_TOOL_CATALOG,
        manifest=projection,
        legacy_boundary={
            "visibility": "forbidden",
            "ordered_adapters": ["synthetic_legacy_tool"],
        },
        compensation={
            "ordered_operations": ["undo:synthetic_tool"],
            "handler_ids": ["synthetic_undo_handler_v1"],
        },
    )

    with pytest.raises(ProjectionError, match="discovery_policy|unsupported"):
        _select(bundle, ToolSelectionSignals(page_kind="workspace"))


def test_tool_selection_result_cannot_be_constructed_outside_selector_issuer() -> None:
    bundle = _bundle()
    contract = next(
        contract
        for contract in _TEST_TOOL_CATALOG.provider_contracts()
        if contract.name == "delete_note"
    )
    result_type = getattr(selector_module, "ToolSelectionResult")

    with pytest.raises(TypeError, match="Selector-issued"):
        result_type(
            provider_contracts=(contract,),
            provider_envelope_fingerprint="0" * 64,
            selected_names=(contract.name,),
            selected_domains=(ToolDomain.NOTES,),
            dependency_closure=(contract.name,),
            full_catalog_fallback=False,
            fallback_reason=None,
            diagnostics=(),
            _bundle_instance_token=bundle.bundle_instance_token,
            _seal=object(),
        )


def test_selector_result_construction_capability_is_owned_only_by_private_issuer() -> None:
    production_root = Path(selector_module.__file__).resolve().parents[1]
    selector_path = Path(selector_module.__file__).resolve()
    authority_surface_path = Path(authority_surface_module.__file__).resolve()
    construction_seal = "_TOOL_SELECTION_RESULT_CONSTRUCTION_SEAL"
    issuer_name = "_issue_tool_selection_result"
    protected_callables = {"ToolSelectionResult", issuer_name}
    allowed_issuer_callers = {
        (selector_path, "select_tools"),
        (authority_surface_path, "intersect_authority_surface"),
    }
    violations: list[str] = []

    assert construction_seal not in getattr(selector_module, "__all__", ())

    for path in production_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        resolved = path.resolve()
        if resolved != selector_path and construction_seal in source:
            violations.append(f"construction seal imported by {path}")

        tree = ast.parse(source, filename=str(path))
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for imported in node.names:
                    if imported.name in protected_callables:
                        aliases[imported.asname or imported.name] = imported.name

        def protected_name(expression: ast.expr) -> str | None:
            if isinstance(expression, ast.Name):
                return aliases.get(expression.id, expression.id)
            if isinstance(expression, ast.Attribute):
                return aliases.get(expression.attr, expression.attr)
            return None

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            referenced = protected_name(value)
            if referenced not in protected_callables:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = referenced

        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callable_name = protected_name(node.func)
            if callable_name not in protected_callables:
                continue
            ancestor = parents.get(node)
            while ancestor is not None and not isinstance(
                ancestor,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                ancestor = parents.get(ancestor)
            owner = (
                ancestor.name
                if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef))
                else None
            )
            if callable_name == "ToolSelectionResult":
                if resolved != selector_path or owner != issuer_name:
                    violations.append(f"result constructed by {path}:{owner}")
            elif (resolved, owner) not in allowed_issuer_callers:
                violations.append(f"private issuer called by {path}:{owner}")

    assert violations == []


def test_selector_private_issuer_rejects_an_incomplete_dependency_closure() -> None:
    bundle = _bundle()
    discovery = bundle.discovery_view()
    authority = bundle.authority_view()
    entry = next(value for value in discovery.ordered_entries if value.provider_name == "get_offer")
    private_issuer = cast(
        Callable[..., object],
        getattr(selector_module, "_issue_tool_selection_result"),
    )

    with pytest.raises(ProjectionError, match="dependency"):
        private_issuer(
            discovery,
            authority,
            provider_contracts=(entry.provider_contract,),
            selected_names=(entry.provider_name,),
            selected_domains=(ToolDomain.OFFERS,),
            dependency_closure=(entry.provider_name,),
            full_catalog_fallback=False,
            fallback_reason=None,
            diagnostics=(),
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("kind", ToolSelectionDiagnosticKind.ATTACHMENT_DOMAIN_COUNT),
        ("kind", "forged_kind"),
        ("count", -1),
        ("count", True),
    ],
)
def test_tool_selection_result_detects_nested_diagnostic_mutation(
    field: str,
    replacement: object,
) -> None:
    bundle = _bundle()
    result = _select(
        bundle,
        ToolSelectionSignals(page_kind="offers", trusted_domains=("offers",)),
    )
    diagnostic = result.diagnostics[0]

    object.__setattr__(diagnostic, field, replacement)

    with pytest.raises(ProjectionError, match="integrity"):
        _ = result.bundle_instance_token


def test_tool_selection_result_detects_diagnostics_tuple_identity_replacement() -> None:
    result = _select(
        _bundle(),
        ToolSelectionSignals(page_kind="offers", trusted_domains=("offers",)),
    )
    replacement = tuple(list(result.diagnostics))
    assert replacement == result.diagnostics
    assert replacement is not result.diagnostics

    object.__setattr__(result, "diagnostics", replacement)

    with pytest.raises(ProjectionError, match="integrity"):
        _ = result.bundle_instance_token


def test_tool_selection_result_detects_diagnostic_element_identity_replacement() -> None:
    result = _select(
        _bundle(),
        ToolSelectionSignals(page_kind="offers", trusted_domains=("offers",)),
    )
    original = result.diagnostics[0]
    replacement_element = ToolSelectionDiagnostic(original.kind, original.count)
    assert replacement_element == original
    assert replacement_element is not original

    object.__setattr__(
        result,
        "diagnostics",
        (replacement_element, *result.diagnostics[1:]),
    )

    with pytest.raises(ProjectionError, match="integrity"):
        _ = result.bundle_instance_token


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("kind", "forged_kind"),
        ("count", -1),
        ("count", True),
    ],
)
def test_selector_private_issuer_rejects_preissued_diagnostic_drift(
    field: str,
    replacement: object,
) -> None:
    bundle = _bundle()
    initial = _select(
        bundle,
        ToolSelectionSignals(page_kind="offers", trusted_domains=("offers",)),
    )
    diagnostic = ToolSelectionDiagnostic(
        ToolSelectionDiagnosticKind.PAGE_DOMAIN_COUNT,
        0,
    )
    object.__setattr__(diagnostic, field, replacement)
    private_issuer = cast(
        Callable[..., object],
        getattr(selector_module, "_issue_tool_selection_result"),
    )

    with pytest.raises((TypeError, ValueError, ProjectionError), match="diagnostic"):
        private_issuer(
            bundle.discovery_view(),
            bundle.authority_view(),
            provider_contracts=initial.provider_contracts,
            selected_names=initial.selected_names,
            selected_domains=initial.selected_domains,
            dependency_closure=initial.dependency_closure,
            full_catalog_fallback=initial.full_catalog_fallback,
            fallback_reason=initial.fallback_reason,
            diagnostics=(diagnostic,),
        )


def test_authority_filter_uses_selector_private_issuer_without_recomputing_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    discovery = bundle.discovery_view()
    authority = bundle.authority_view()
    initial = select_tools(
        discovery,
        authority,
        ToolSelectionSignals(page_kind="workspace", trusted_domains=("offers",)),
    )
    offers_read = authority.entries["list_offers"].required_capabilities[0]
    view = AuthoritySurfaceView(
        capability_profile_id=PROFILE_ID,
        capability_policy_version=CAPABILITY_POLICY_VERSION,
        dependency_policy_version=DEPENDENCY_POLICY_VERSION,
        capabilities=frozenset({offers_read}),
        context_type="workspace",
    )
    private_issuer = getattr(selector_module, "_issue_tool_selection_result", None)
    assert callable(private_issuer), "filtered selections require the Selector's private issuer"
    issuer_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def recording_issuer(*args: object, **kwargs: object) -> object:
        issuer_calls.append((args, kwargs))
        return cast(Callable[..., object], private_issuer)(*args, **kwargs)

    def forbidden_authority_recompute(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Authority must not materialize or hash Provider envelopes")

    monkeypatch.setattr(
        authority_surface_module,
        "_issue_tool_selection_result",
        recording_issuer,
        raising=False,
    )
    monkeypatch.setattr(
        authority_surface_module,
        "materialize_provider_payloads",
        forbidden_authority_recompute,
        raising=False,
    )
    monkeypatch.setattr(
        authority_surface_module,
        "sha256_hex",
        forbidden_authority_recompute,
        raising=False,
    )

    filtered = intersect_authority_surface(discovery, authority, initial, view)
    expected_payloads = selector_module.materialize_provider_payloads(filtered.provider_contracts)
    expected_fingerprint = sha256_hex(canonical_json(expected_payloads))

    assert len(issuer_calls) == 1
    assert filtered.selected_names == ("list_offers", "get_offer", "compare_offers")
    assert filtered.provider_envelope_fingerprint == expected_fingerprint
