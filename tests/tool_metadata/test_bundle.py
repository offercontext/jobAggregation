from __future__ import annotations

import copy
import hashlib
import json
import pickle
import re
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping, Sequence
from dataclasses import asdict, fields, is_dataclass, replace
from types import MappingProxyType
from typing import Any, cast

import pytest

from offerpilot.ai.tool_runtime import metadata as metadata_module
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from tests.tool_metadata.factories import (
    compose_synthetic_bundle,
    synthetic_tool_spec,
)


_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_VIEW_FIELDS = {
    "ProviderToolMetadataView": {
        "ordered_contracts",
        "provider_boundary_fingerprint",
        "bundle_instance_token",
    },
    "ToolDiscoveryMetadataView": {
        "ordered_entries",
        "policy",
        "discovery_fingerprint",
        "bundle_instance_token",
    },
    "ToolAuthorityMetadataView": {
        "entries",
        "authority_manifest_fingerprint",
        "bundle_instance_token",
    },
    "ToolOperationMetadataView": {
        "entries",
        "operation_fingerprint",
        "bundle_instance_token",
    },
    "LegacyDeterministicBoundaryV1": {
        "ordered_adapter_bindings",
        "initial_route_bindings",
        "legacy_boundary_fingerprint",
        "bundle_instance_token",
    },
    "CompensationMetadataView": {
        "ordered_handler_bindings",
        "compensation_fingerprint",
        "bundle_instance_token",
    },
}


def _required_api(name: str) -> Any:
    value = getattr(metadata_module, name, None)
    assert value is not None, f"Task 5 API is missing: {name}"
    return value


def _bundle() -> tuple[object, ToolCatalog, dict[str, object]]:
    spec = synthetic_tool_spec()
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    source = compose_synthetic_bundle()
    bundle_type = _required_api("ToolMetadataBundleV1")
    bundle = bundle_type(
        typed_catalog=catalog,
        manifest=source["manifest"],
        legacy_boundary=source["legacy_boundary"],
        compensation=source["compensation"],
    )
    return bundle, catalog, source


def _views(bundle: object) -> tuple[object, ...]:
    return (
        bundle.provider_view(),  # type: ignore[attr-defined]
        bundle.discovery_view(),  # type: ignore[attr-defined]
        bundle.authority_view(),  # type: ignore[attr-defined]
        bundle.operation_view(),  # type: ignore[attr-defined]
        bundle.legacy_boundary(),  # type: ignore[attr-defined]
        bundle.compensation_view(),  # type: ignore[attr-defined]
    )


def _view_fingerprints(bundle: object) -> tuple[str, ...]:
    return tuple(
        getattr(view, name)
        for view, name in zip(
            _views(bundle),
            (
                "provider_boundary_fingerprint",
                "discovery_fingerprint",
                "authority_manifest_fingerprint",
                "operation_fingerprint",
                "legacy_boundary_fingerprint",
                "compensation_fingerprint",
            ),
        )
    )


def _canonical_fingerprint(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _field_names(value: object) -> set[str]:
    assert is_dataclass(value), f"{type(value).__name__} must be a frozen dataclass view"
    return {item.name for item in fields(value)}


def _assert_frozen_tree(value: object) -> None:
    if isinstance(value, Mapping):
        with pytest.raises((AttributeError, TypeError)):
            value["__mutation_probe__"] = object()  # type: ignore[index]
        for item in value.values():
            _assert_frozen_tree(item)
        return
    if isinstance(value, tuple):
        for item in value:
            _assert_frozen_tree(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        pytest.fail(f"mutable sequence escaped into a Bundle view: {type(value).__name__}")


def _assert_no_catalog_reference(value: object, catalog: ToolCatalog) -> None:
    assert value is not catalog
    assert not isinstance(value, ToolCatalog)
    if is_dataclass(value):
        for item in fields(value):
            _assert_no_catalog_reference(getattr(value, item.name), catalog)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _assert_no_catalog_reference(key, catalog)
            _assert_no_catalog_reference(item, catalog)
    elif isinstance(value, tuple):
        for item in value:
            _assert_no_catalog_reference(item, catalog)


def test_bundle_exposes_only_the_six_exact_narrow_view_shapes() -> None:
    bundle, catalog, _ = _bundle()
    views = _views(bundle)

    assert tuple(type(view).__name__ for view in views) == tuple(_VIEW_FIELDS)
    for view in views:
        assert _field_names(view) == _VIEW_FIELDS[type(view).__name__]
        _assert_no_catalog_reference(view, catalog)

    provider = views[0]
    assert provider.ordered_contracts == catalog.provider_contracts()  # type: ignore[attr-defined]
    assert provider.ordered_contracts[0] is catalog.specs[0].contract  # type: ignore[attr-defined]


def test_all_views_share_one_exact_bundle_token_but_another_bundle_does_not() -> None:
    first, _, _ = _bundle()
    second, _, _ = _bundle()

    first_tokens = tuple(
        view.bundle_instance_token  # type: ignore[attr-defined]
        for view in _views(first)
    )
    second_tokens = tuple(
        view.bundle_instance_token  # type: ignore[attr-defined]
        for view in _views(second)
    )

    assert all(token is first_tokens[0] for token in first_tokens)
    assert all(token is second_tokens[0] for token in second_tokens)
    assert first_tokens[0] is not second_tokens[0]
    assert first_tokens[0] != second_tokens[0]


def test_bundle_fingerprint_is_canonical_static_manifest_identity() -> None:
    first, _, first_source = _bundle()
    second, _, second_source = _bundle()

    assert first.bundle_fingerprint == _canonical_fingerprint(  # type: ignore[attr-defined]
        first_source["manifest"]
    )
    assert second.bundle_fingerprint == _canonical_fingerprint(  # type: ignore[attr-defined]
        second_source["manifest"]
    )
    assert first.bundle_fingerprint == second.bundle_fingerprint  # type: ignore[attr-defined]
    assert _FINGERPRINT.fullmatch(first.bundle_fingerprint)  # type: ignore[attr-defined]
    assert "bundle_fingerprint" not in cast(dict[str, object], first_source["manifest"])


def test_view_fingerprints_ignore_object_identity_and_cover_canonical_fields() -> None:
    first, _, _ = _bundle()
    second, _, _ = _bundle()
    assert _view_fingerprints(first) == _view_fingerprints(second)

    spec = synthetic_tool_spec()
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    bundle_type = _required_api("ToolMetadataBundleV1")
    legacy_source = compose_synthetic_bundle()
    cast(dict[str, object], legacy_source["legacy_boundary"])["ordered_adapters"] = (
        "different_legacy",
    )
    changed_legacy = bundle_type(
        typed_catalog=catalog,
        manifest=legacy_source["manifest"],
        legacy_boundary=legacy_source["legacy_boundary"],
        compensation=legacy_source["compensation"],
    )
    compensation_source = compose_synthetic_bundle()
    cast(dict[str, object], compensation_source["compensation"])["handler_ids"] = (
        "different_handler_v1",
    )
    changed_compensation = bundle_type(
        typed_catalog=catalog,
        manifest=compensation_source["manifest"],
        legacy_boundary=compensation_source["legacy_boundary"],
        compensation=compensation_source["compensation"],
    )

    assert (
        changed_legacy.legacy_boundary().legacy_boundary_fingerprint
        != first.legacy_boundary().legacy_boundary_fingerprint
    )
    assert (
        changed_compensation.compensation_view().compensation_fingerprint
        != first.compensation_view().compensation_fingerprint
    )


def test_concurrent_bundle_reads_preserve_exact_view_and_nested_identity() -> None:
    bundle, _, _ = _bundle()
    expected_views = _views(bundle)

    def snapshot() -> tuple[object, ...]:
        views = _views(bundle)
        return (
            *views,
            views[0].bundle_instance_token,  # type: ignore[attr-defined]
            views[1].policy,  # type: ignore[attr-defined]
            views[1].ordered_entries,  # type: ignore[attr-defined]
            bundle.bundle_fingerprint,  # type: ignore[attr-defined]
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        snapshots = tuple(executor.map(lambda _: snapshot(), range(64)))

    expected = snapshot()
    for observed in snapshots:
        assert all(actual is stable for actual, stable in zip(observed[:-1], expected[:-1]))
        assert observed[-1] == expected[-1]
    assert all(snapshot()[index] is view for index, view in enumerate(expected_views))


def test_bundle_copies_and_recursively_freezes_every_narrow_projection() -> None:
    bundle, _, source = _bundle()
    views = _views(bundle)
    fingerprints = _view_fingerprints(bundle)
    assert all(_FINGERPRINT.fullmatch(value) for value in fingerprints)

    for view in views:
        for item in fields(view):
            if item.name != "bundle_instance_token":
                _assert_frozen_tree(getattr(view, item.name))
        with pytest.raises((AttributeError, TypeError)):
            setattr(view, next(iter(_VIEW_FIELDS[type(view).__name__])), object())

    manifest = cast(dict[str, object], source["manifest"])
    discovery = cast(dict[str, object], manifest["discovery_policy"])
    discovery["provider_visibility"] = "mutated-after-composition"
    legacy = cast(dict[str, object], source["legacy_boundary"])
    legacy["visibility"] = "mutated-after-composition"
    compensation = cast(dict[str, object], source["compensation"])
    compensation["ordered_operations"] = ("mutated",)

    assert _view_fingerprints(bundle) == fingerprints


def test_bundle_accessors_fail_closed_after_same_value_view_field_replacement() -> None:
    replacements = (
        ("provider_view", "ordered_contracts"),
        ("discovery_view", "ordered_entries"),
        ("authority_view", "entries"),
        ("operation_view", "entries"),
        ("legacy_boundary", "ordered_adapter_bindings"),
        ("compensation_view", "ordered_handler_bindings"),
    )

    for accessor_name, field_name in replacements:
        bundle, _, _ = _bundle()
        accessor = getattr(bundle, accessor_name)
        view = accessor()
        original = getattr(view, field_name)
        replacement = (
            MappingProxyType(dict(original))
            if isinstance(original, Mapping)
            else tuple(list(original))
        )
        assert replacement == original
        assert replacement is not original
        object.__setattr__(view, field_name, replacement)

        with pytest.raises((TypeError, ValueError), match="integrity|drift"):
            getattr(view, field_name)
        with pytest.raises((TypeError, ValueError), match="integrity|drift"):
            accessor()


def test_direct_view_read_fails_closed_after_nested_entry_replacement() -> None:
    bundle, _, _ = _bundle()
    view = bundle.discovery_view()  # type: ignore[attr-defined]
    entry = view.ordered_entries[0]
    replacement = tuple(list(entry.domains))
    assert replacement == entry.domains
    assert replacement is not entry.domains
    object.__setattr__(entry, "domains", replacement)

    with pytest.raises((TypeError, ValueError), match="integrity|drift"):
        view.ordered_entries


def test_any_view_drift_revokes_the_complete_bundle_and_new_segment_leases() -> None:
    bundle, _, _ = _bundle()
    provider = bundle.provider_view()  # type: ignore[attr-defined]
    object.__setattr__(provider, "ordered_contracts", ())

    probes = (
        lambda: bundle.bundle_instance_token,  # type: ignore[attr-defined]
        lambda: bundle.bundle_fingerprint,  # type: ignore[attr-defined]
        bundle.open_segment_lease,  # type: ignore[attr-defined]
        bundle.discovery_view,  # type: ignore[attr-defined]
    )
    for probe in probes:
        with pytest.raises((TypeError, ValueError), match="integrity|drift"):
            probe()


@pytest.mark.parametrize("operation", (copy.copy, copy.deepcopy, pickle.dumps, asdict))
def test_bundle_and_views_reject_copy_pickle_and_dataclass_serialization(
    operation: object,
) -> None:
    bundle, _, _ = _bundle()
    for value in (bundle, *_views(bundle)):
        with pytest.raises((TypeError, ValueError)):
            operation(value)  # type: ignore[operator]


def test_bundle_and_views_reject_generic_serialization_and_safe_repr() -> None:
    bundle, _, _ = _bundle()
    for value in (bundle, *_views(bundle)):
        with pytest.raises((TypeError, ValueError)):
            json.dumps(value)
        with pytest.raises((TypeError, ValueError)):
            value.to_json()  # type: ignore[attr-defined]
        representation = repr(value)
        assert "0x" not in representation
        assert "function" not in representation
        assert "callable" not in representation
        assert "<lambda>" not in representation


def test_callers_cannot_construct_a_valid_view_or_bundle_token() -> None:
    bundle, _, _ = _bundle()
    for view in _views(bundle):
        constructor_values = {item.name: getattr(view, item.name) for item in fields(view)}
        with pytest.raises((TypeError, ValueError)):
            type(view)(**constructor_values)
        with pytest.raises((TypeError, ValueError)):
            replace(view)

    token = _views(bundle)[0].bundle_instance_token  # type: ignore[attr-defined]
    with pytest.raises((TypeError, ValueError)):
        type(token)()


def test_published_bundle_cannot_be_reinitialized() -> None:
    bundle, catalog, source = _bundle()
    token = bundle.bundle_instance_token  # type: ignore[attr-defined]
    views = _views(bundle)

    with pytest.raises((TypeError, ValueError), match="initialized|sealed|immutable"):
        bundle.__init__(  # type: ignore[misc]
            typed_catalog=catalog,
            manifest=source["manifest"],
            legacy_boundary=source["legacy_boundary"],
            compensation=source["compensation"],
        )

    assert bundle.bundle_instance_token is token  # type: ignore[attr-defined]
    assert all(actual is expected for actual, expected in zip(_views(bundle), views))


def test_bundle_rejects_view_factory_token_replacement_before_lease_issue() -> None:
    bundle, _, _ = _bundle()
    token = bundle.bundle_instance_token  # type: ignore[attr-defined]
    factory = object.__getattribute__(bundle, "_view_factory")
    replacement = object.__new__(type(token))
    object.__setattr__(factory, "_bundle_instance_token", replacement)

    with pytest.raises((TypeError, ValueError), match="integrity|drift"):
        bundle.bundle_instance_token  # type: ignore[attr-defined]
    with pytest.raises((TypeError, ValueError), match="integrity|drift"):
        bundle.open_segment_lease()  # type: ignore[attr-defined]
