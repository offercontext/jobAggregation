from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from offerpilot.ai.tool_runtime.contracts import ToolFailure, ToolSpec
from offerpilot.ai.tool_runtime.metadata import (
    ResolverImplementationBinding,
    ToolPresentationBindingV1,
)
from offerpilot.ai.tool_runtime.policy_types import UndoPolicy
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.tool_runtime.transport import project_transport_event

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


@pytest.mark.parametrize(
    ("tool_name", "result", "expected"),
    (
        (
            "create_application",
            {
                "application_id": 7,
                "company_name": "牛客网",
                "position_name": "软件测试工程师",
            },
            "✅ 创建成功：投递记录 #7 已保存（牛客网 · 软件测试工程师）。",
        ),
        (
            "add_note",
            {
                "note_id": 8,
                "company": "牛客网",
                "position": "软件测试工程师",
                "round": "技术一面",
            },
            "✅ 保存成功：复盘记录 #8 已保存（牛客网 · 软件测试工程师 · 技术一面）。",
        ),
        (
            "create_application_event",
            {"application_event_id": 9},
            "✅ 创建成功：日程 #9 已保存。",
        ),
    ),
)
def test_special_write_success_summaries_are_owned_by_exact_presentation_binding(
    tool_name: str,
    result: dict[str, object],
    expected: str,
) -> None:
    spec = MODEL_TOOL_CATALOG.resolve(tool_name)
    assert spec is not None
    assert spec.presentation.success_summary_projector(result) == expected


def test_deterministic_has_no_legacy_presentation_or_editability_shadow() -> None:
    from offerpilot.pilot_runtime import deterministic

    source = inspect.getsource(deterministic)
    assert not hasattr(deterministic, "_LEGACY_EDITABLE_FIELDS")
    assert "def _pending_details(" not in source
    assert "def _effective_legacy_args(" not in source


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


def test_every_declared_failure_renderer_and_transport_shape_is_public_and_bounded() -> None:
    sentinel = "raw-exception-and-arguments-must-not-leak"
    for spec in MODEL_TOOL_CATALOG.specs:
        for category in spec.declared_failure_categories:
            failure = ToolFailure(category=category, code=f"{spec.name}_{category}")
            visible = render_compatibility(spec, failure)
            record = SimpleNamespace(
                prepared=SimpleNamespace(tool_call_id=f"call-{spec.name}"),
                outcome=failure,
            )
            payload = project_transport_event(spec, record)

            assert visible.startswith("错误：")
            assert payload == {
                "tool_call_id": f"call-{spec.name}",
                "tool_name": spec.name,
                "status": "error",
                "summary": visible[:500],
                "evidence": [],
                "affected_resources": [],
                "changed_entities": [],
            }
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            assert sentinel not in encoded
            assert len(encoded.encode("utf-8")) <= 128 * 1024


def test_typed_presentation_is_resolved_only_through_an_exact_live_route_handle() -> None:
    from tests.tool_metadata.test_production_bundle import _production_components

    components = _production_components()
    lease = components.bundle.open_segment_lease()
    try:
        for expected in MODEL_TOOL_CATALOG.specs:
            route_handle = lease.resolve(expected.name)
            assert route_handle is not None
            resolved = lease.require_spec(route_handle)
            assert resolved is expected
            assert resolved.presentation is expected.presentation
            assert resolved.undo_builder_binding is expected.undo_builder_binding
    finally:
        lease.close()

    with pytest.raises((RuntimeError, TypeError, ValueError), match="closed|revoked"):
        lease.require_spec(route_handle)
