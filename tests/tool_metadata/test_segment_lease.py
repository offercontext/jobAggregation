from __future__ import annotations

import copy
import json
import pickle
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

import pytest

from offerpilot.ai.tool_runtime import catalog as catalog_module
from offerpilot.ai.tool_runtime import metadata as metadata_module
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from tests.tool_metadata.factories import (
    compose_synthetic_bundle,
    forbid_call,
    synthetic_tool_spec,
)


def _required_api(name: str) -> Any:
    value = getattr(metadata_module, name, None)
    assert value is not None, f"Task 5 API is missing: {name}"
    return value


def _bundle() -> tuple[object, ToolCatalog, object]:
    spec = synthetic_tool_spec()
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    bundle_type = _required_api("ToolMetadataBundleV1")
    source = compose_synthetic_bundle()
    bundle = bundle_type(
        typed_catalog=catalog,
        manifest=source["manifest"],
        legacy_boundary=source["legacy_boundary"],
        compensation=source["compensation"],
    )
    return bundle, catalog, spec


def test_segment_leases_are_fresh_monotonic_and_share_the_bundle_catalog() -> None:
    bundle, _, spec = _bundle()
    first = bundle.open_segment_lease()  # type: ignore[attr-defined]
    second = bundle.open_segment_lease()  # type: ignore[attr-defined]
    first_handle = first.resolve(spec.name)
    second_handle = second.resolve(spec.name)

    assert first is not second
    assert first.generation < second.generation
    assert first.segment_catalog_token is not second.segment_catalog_token
    assert first.bundle_instance_token is second.bundle_instance_token
    assert first.bundle_instance_token is bundle.provider_view().bundle_instance_token  # type: ignore[attr-defined]
    assert first_handle is not second_handle
    assert first_handle.segment_catalog_token is first.segment_catalog_token
    assert second_handle.segment_catalog_token is second.segment_catalog_token
    assert first_handle.bundle_instance_token is first.bundle_instance_token
    assert second_handle.bundle_instance_token is second.bundle_instance_token
    assert first.require_spec(first_handle) is spec
    assert second.require_spec(second_handle) is spec
    assert first.require_spec(first_handle).metadata is second.require_spec(second_handle).metadata


def test_closed_lease_rejects_resolution_and_handles_and_close_is_idempotent() -> None:
    bundle, _, spec = _bundle()
    lease = bundle.open_segment_lease()  # type: ignore[attr-defined]
    handle = lease.resolve(spec.name)

    lease.close()
    lease.close()

    assert lease.closed is True
    with pytest.raises((RuntimeError, ValueError), match="closed|lease|revoked"):
        lease.resolve(spec.name)
    with pytest.raises((RuntimeError, ValueError), match="closed|lease|revoked"):
        lease.require_spec(handle)


def test_cross_segment_and_cross_bundle_handles_are_rejected() -> None:
    first_bundle, _, spec = _bundle()
    second_bundle, _, _ = _bundle()
    first = first_bundle.open_segment_lease()  # type: ignore[attr-defined]
    sibling = first_bundle.open_segment_lease()  # type: ignore[attr-defined]
    foreign = second_bundle.open_segment_lease()  # type: ignore[attr-defined]
    handle = first.resolve(spec.name)

    assert first.require_spec(handle) is spec
    with pytest.raises((RuntimeError, ValueError), match="segment|lease|provenance"):
        sibling.require_spec(handle)
    with pytest.raises((RuntimeError, ValueError), match="bundle|lease|provenance"):
        foreign.require_spec(handle)


def test_bundle_view_drift_revokes_existing_segment_handles_and_lease() -> None:
    bundle, _, spec = _bundle()
    lease = bundle.open_segment_lease()  # type: ignore[attr-defined]
    handle = lease.resolve(spec.name)
    provider = bundle.provider_view()  # type: ignore[attr-defined]
    object.__setattr__(provider, "ordered_contracts", ())

    with pytest.raises((RuntimeError, ValueError), match="integrity|drift|revoked"):
        lease.resolve(spec.name)
    with pytest.raises((RuntimeError, ValueError), match="integrity|drift|revoked"):
        lease.require_spec(handle)
    with pytest.raises((RuntimeError, ValueError), match="integrity|drift|revoked"):
        handle.tool_name


def test_open_and_resolve_do_not_deepcopy_recompile_or_rebuild_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _, spec = _bundle()

    monkeypatch.setattr(copy, "deepcopy", forbid_call)
    monkeypatch.setattr(catalog_module, "compile_tool_metadata_manifest", forbid_call)
    monkeypatch.setattr(ToolCatalog, "__init__", forbid_call)

    lease = bundle.open_segment_lease()  # type: ignore[attr-defined]
    handle = lease.resolve(spec.name)

    assert lease.require_spec(handle) is spec


@pytest.mark.parametrize("operation", (copy.copy, copy.deepcopy, pickle.dumps, asdict))
def test_segment_lease_token_and_handle_reject_copy_pickle_and_asdict(
    operation: object,
) -> None:
    bundle, _, spec = _bundle()
    lease = bundle.open_segment_lease()  # type: ignore[attr-defined]
    handle = lease.resolve(spec.name)
    values = (
        lease,
        lease.bundle_instance_token,
        lease.segment_catalog_token,
        handle,
    )

    for value in values:
        with pytest.raises((TypeError, ValueError)):
            operation(value)  # type: ignore[operator]


def test_segment_transients_reject_generic_serialization_and_safe_repr() -> None:
    bundle, _, spec = _bundle()
    lease = bundle.open_segment_lease()  # type: ignore[attr-defined]
    handle = lease.resolve(spec.name)

    for value in (
        lease,
        lease.bundle_instance_token,
        lease.segment_catalog_token,
        handle,
    ):
        with pytest.raises((TypeError, ValueError)):
            json.dumps(value)
        with pytest.raises((TypeError, ValueError)):
            value.to_json()
        representation = repr(value)
        assert "0x" not in representation
        assert "function" not in representation
        assert "callable" not in representation
        assert "<lambda>" not in representation


def test_missing_tool_never_creates_a_route_handle() -> None:
    bundle, _, _ = _bundle()
    lease = bundle.open_segment_lease()  # type: ignore[attr-defined]

    assert lease.resolve("not_in_this_catalog") is None


def test_concurrent_lease_generations_are_unique_and_contiguous() -> None:
    bundle, _, _ = _bundle()

    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = tuple(executor.map(lambda _: bundle.open_segment_lease(), range(64)))  # type: ignore[attr-defined]

    assert sorted(lease.generation for lease in leases) == list(range(1, 65))
    assert len({id(lease.segment_catalog_token) for lease in leases}) == len(leases)


def test_close_linearizes_against_resolve_and_revokes_every_returned_handle() -> None:
    bundle, _, spec = _bundle()
    lease = bundle.open_segment_lease()  # type: ignore[attr-defined]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = tuple(executor.submit(lease.resolve, spec.name) for _ in range(32))
        close_futures = tuple(executor.submit(lease.close) for _ in range(8))

    for future in close_futures:
        assert future.result() is None
    assert lease.closed is True
    for future in futures:
        try:
            handle = future.result()
        except (RuntimeError, ValueError):
            continue
        if handle is not None:
            with pytest.raises((RuntimeError, ValueError), match="closed|lease|revoked"):
                lease.require_spec(handle)


def test_forged_handle_with_copied_fields_is_not_registered() -> None:
    bundle, _, spec = _bundle()
    lease = bundle.open_segment_lease()  # type: ignore[attr-defined]
    handle = lease.resolve(spec.name)
    handle_type = type(handle)
    forged = object.__new__(handle_type)

    for name in getattr(handle_type, "__slots__", ()):
        if name == "__weakref__":
            continue
        object.__setattr__(forged, name, object.__getattribute__(handle, name))

    with pytest.raises((RuntimeError, ValueError), match="segment|lease|provenance"):
        lease.require_spec(forged)
