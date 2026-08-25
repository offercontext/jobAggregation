from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, replace

import pytest

from offerpilot.ai.tool_runtime.contracts import ToolSpec
from offerpilot.ai.tool_runtime.metadata import (
    ResolverImplementationBinding,
    ToolPresentationBindingV1,
)
from offerpilot.ai.tool_runtime.policy_types import UndoPolicy
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG

from .factories import (
    forbid_call,
    read_metadata,
    resolver_descriptor,
    synthetic_tool_spec,
    write_metadata,
)


LEGACY_TOP_LEVEL_FIELDS = (
    "kind",
    "required_capabilities",
    "binding_contract",
    "binding_resolvers",
    "confirmation_policy",
    "editable_fields",
    "write_contract",
)
FINAL_RUNTIME_FIELDS = (
    "metadata",
    "resolver_bindings",
    "undo_builder_binding",
    "presentation",
)


@dataclass
class PresentationProbe:
    confirmation_calls: int = 0
    pending_calls: int = 0
    success_calls: int = 0
    cancelled: bool = False

    def reset(self) -> None:
        self.confirmation_calls = 0
        self.pending_calls = 0
        self.success_calls = 0
        self.cancelled = False


_PRESENTATION_PROBE = PresentationProbe()


def _probe_confirmation_description(args: object) -> str:
    del args
    _PRESENTATION_PROBE.confirmation_calls += 1
    return "probe confirmation"


def _probe_pending_details(args: object) -> dict[str, object]:
    del args
    _PRESENTATION_PROBE.pending_calls += 1
    return {"kind": "probe pending"}


def _probe_success_summary(result: object) -> str:
    del result
    _PRESENTATION_PROBE.success_calls += 1
    if _PRESENTATION_PROBE.cancelled:
        raise asyncio.CancelledError()
    return "probe success"


def test_final_tool_spec_shape_has_metadata_and_no_legacy_forwarding_fields() -> None:
    spec = synthetic_tool_spec()
    assert isinstance(spec, ToolSpec)
    for field_name in FINAL_RUNTIME_FIELDS:
        assert hasattr(spec, field_name)
    for field_name in LEGACY_TOP_LEVEL_FIELDS:
        assert not hasattr(spec, field_name)
    assert spec.name == spec.contract.name


def test_every_production_spec_has_complete_named_presentation_binding() -> None:
    specs = MODEL_TOOL_CATALOG.specs
    assert len(specs) == 25
    for spec in specs:
        presentation = spec.presentation
        assert isinstance(presentation, ToolPresentationBindingV1)
        assert presentation.implementation_id
        callbacks = (
            presentation.confirmation_description,
            presentation.pending_details_projector,
            presentation.success_summary_projector,
        )
        assert all(inspect.isfunction(callback) for callback in callbacks)
        assert all(callback.__name__ != "<lambda>" for callback in callbacks)
        assert all("<locals>" not in callback.__qualname__ for callback in callbacks)


def test_presentation_binding_is_not_inferred_from_tool_name() -> None:
    first = synthetic_tool_spec("synthetic_first")
    second = synthetic_tool_spec("synthetic_second")
    assert first.presentation is not second.presentation
    assert first.presentation.implementation_id == second.presentation.implementation_id

    # A binding carrying the forbidden callable must still be rejected by the
    # binding constructor/validator, rather than being replaced by a name map.
    with pytest.raises((TypeError, ValueError), match="named|implementation|presentation"):
        ToolPresentationBindingV1(
            implementation_id="synthetic_forbidden",
            confirmation_description=lambda args: "forbidden",
            pending_details_projector=forbid_call,
            success_summary_projector=forbid_call,
        )


def test_presentation_callable_replacement_after_catalog_seal_fails_closed() -> None:
    spec = synthetic_tool_spec(
        "synthetic_presentation_seal",
        metadata=read_metadata(resolver_descriptors=(resolver_descriptor(),)),
    )
    from offerpilot.ai.tool_runtime.catalog import ToolCatalog

    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    assert catalog.resolve(spec.name) is spec
    object.__setattr__(spec.presentation, "success_summary_projector", forbid_call)
    with pytest.raises(
        (TypeError, ValueError), match="presentation|callable|identity|seal|integrity"
    ):
        catalog.resolve(spec.name)


def test_complete_presentation_replacement_seals_probe_state_and_cancellation() -> None:
    from offerpilot.ai.tool_runtime.catalog import ToolCatalog

    _PRESENTATION_PROBE.reset()
    try:
        original = synthetic_tool_spec("synthetic_presentation_replacement")
        replacement = ToolPresentationBindingV1(
            implementation_id="synthetic_presentation_probe_v1",
            confirmation_description=_probe_confirmation_description,
            pending_details_projector=_probe_pending_details,
            success_summary_projector=_probe_success_summary,
        )
        assert replacement is not original.presentation
        assert replacement._identity_snapshot is not original.presentation._identity_snapshot

        replaced = replace(original, presentation=replacement)
        catalog = ToolCatalog((replaced,), expected_names=(replaced.name,))
        resolved = catalog.resolve(replaced.name)
        assert resolved is replaced
        assert resolved is not None

        resolved.presentation.confirmation_description({})
        assert resolved.presentation.pending_details_projector({}) == {"kind": "probe pending"}
        assert resolved.presentation.success_summary_projector({"ok": True}) == "probe success"
        assert (_PRESENTATION_PROBE.confirmation_calls, _PRESENTATION_PROBE.pending_calls) == (1, 1)
        assert _PRESENTATION_PROBE.success_calls == 1

        _PRESENTATION_PROBE.cancelled = True
        with pytest.raises(asyncio.CancelledError):
            resolved.presentation.success_summary_projector({"ok": True})
        assert _PRESENTATION_PROBE.success_calls == 2

        object.__setattr__(replacement, "pending_details_projector", forbid_call)
        with pytest.raises(
            (TypeError, ValueError), match="presentation|callable|identity|seal|integrity"
        ):
            catalog.resolve(replaced.name)
    finally:
        _PRESENTATION_PROBE.reset()


def test_resolver_and_undo_bindings_are_direct_runtime_fields() -> None:
    resolver_spec = synthetic_tool_spec(
        "synthetic_runtime_fields",
        metadata=read_metadata(resolver_descriptors=(resolver_descriptor(),)),
    )
    assert len(resolver_spec.resolver_bindings) == 1
    assert isinstance(resolver_spec.resolver_bindings[0], ResolverImplementationBinding)
    assert (
        resolver_spec.resolver_bindings[0].descriptor
        is resolver_spec.metadata.binding.resolver_descriptors[0]
    )

    write_spec = synthetic_tool_spec(
        "synthetic_required_undo",
        metadata=write_metadata(undo_policy=UndoPolicy.REQUIRED),
    )
    assert write_spec.undo_builder_binding is not None
    assert write_spec.undo_builder_binding.descriptor is write_spec.metadata.operation
