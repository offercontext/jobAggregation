from __future__ import annotations

from enum import Enum

import pytest

from offerpilot.ai.tool_authority.contracts import _require_capabilities
from offerpilot.ai.tool_runtime.policy_types import (
    CompensationKind,
    LegacyBoundaryVisibility,
    OperationKind,
    ProviderVisibility,
    ToolCapability,
    ToolDomain,
    UndoPayloadKind,
    UndoPolicy,
)


def _values(enum_type: type[Enum]) -> tuple[object, ...]:
    return tuple(item.value for item in enum_type)


def test_policy_enums_are_exact_closed_string_contracts() -> None:
    assert _values(ToolDomain) == (
        "applications",
        "events",
        "jd",
        "notes",
        "offers",
        "resumes",
    )
    assert _values(ToolCapability) == (
        "applications.read",
        "applications.write",
        "application_events.read",
        "application_events.write",
        "notes.read",
        "notes.write",
        "offers.read",
        "offers.write",
        "resumes.read",
        "resumes.write",
        "jd_analyses.read",
    )
    assert _values(ProviderVisibility) == ("model_eligible",)
    assert _values(LegacyBoundaryVisibility) == ("forbidden",)
    assert _values(OperationKind) == ("read", "transactional_write")
    assert _values(UndoPolicy) == ("none", "required")
    assert _values(UndoPayloadKind) == (
        "delete_application",
        "update_application_status",
        "delete_application_event",
        "delete_note",
    )
    assert _values(CompensationKind) == (
        "undo:create_application",
        "undo:update_application_status",
        "undo:create_application_event",
        "undo:add_note",
    )

    for enum_type in (
        ToolDomain,
        ToolCapability,
        ProviderVisibility,
        LegacyBoundaryVisibility,
        OperationKind,
        UndoPolicy,
        UndoPayloadKind,
        CompensationKind,
    ):
        assert issubclass(enum_type, str)
        with pytest.raises(ValueError):
            enum_type("unknown")


def test_tool_capability_has_one_leaf_identity_and_no_context_reexport() -> None:
    from offerpilot.ai.tool_runtime import context, policy_types, ToolCapability as public_type

    assert ToolCapability is policy_types.ToolCapability
    assert public_type is ToolCapability
    assert ToolCapability.__module__ == "offerpilot.ai.tool_runtime.policy_types"
    assert not hasattr(context, "ToolCapability")


def test_authority_accepts_only_the_relocated_exact_capability_enum() -> None:
    accepted = frozenset(
        {
            ToolCapability.APPLICATIONS_READ,
            ToolCapability.APPLICATIONS_WRITE,
        }
    )
    assert _require_capabilities(accepted) is accepted

    class LookalikeCapability(str, Enum):
        APPLICATIONS_READ = "applications.read"

    with pytest.raises(ValueError, match="closed V1 capability set"):
        _require_capabilities(frozenset({LookalikeCapability.APPLICATIONS_READ}))

    spoof_type = Enum(
        "ToolCapability",
        {"APPLICATIONS_READ": "applications.read"},
        type=str,
        module="offerpilot.ai.tool_runtime.policy_types",
    )
    spoof = next(iter(spoof_type))
    assert type(spoof).__module__ == ToolCapability.__module__
    assert type(spoof).__name__ == ToolCapability.__name__
    assert type(spoof) is not ToolCapability
    with pytest.raises(ValueError, match="closed V1 capability set"):
        _require_capabilities(frozenset({spoof}))


@pytest.mark.parametrize(
    "value",
    (
        "applications.read\x00",
        "applications.read\x1f",
        "applications.read\x7f",
        "applications.read\x80",
        "applications.read\x9f",
        "applications.read" + "x" * 256,
    ),
)
def test_closed_policy_text_rejects_control_or_unbounded_lookalikes(value: str) -> None:
    with pytest.raises(ValueError):
        ToolCapability(value)
