from importlib import import_module

from offerpilot.ai.tool_runtime.policy_types import ToolCapability
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    BindingContract,
    BindingResolverSpec,
    BindingTarget,
    ConfirmationRequired,
    PreparedToolCall,
    ProviderToolContract,
    ReadyToExecute,
    ToolExecutionRecord,
    ToolFailure,
    ToolResultMetadata,
    ToolSpec,
    ToolSuccess,
)
from offerpilot.ai.tool_runtime.context import (
    UNAVAILABLE,
    ToolExecutionContext,
    aggregate_binding,
    evaluate_context,
)
from offerpilot.ai.tool_runtime.validation import (
    ArgumentValidationError,
    SchemaContractError,
    canonical_json,
    compile_tool_schema,
    parse_arguments,
    validate_arguments,
)
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.tool_runtime.transport import project_transport_event

__all__ = [
    "ArgumentValidationError",
    "BindingAudit",
    "BindingContract",
    "BindingResolverSpec",
    "BindingTarget",
    "ConfirmationRequired",
    "PreparedToolCall",
    "ProviderToolContract",
    "ReadyToExecute",
    "Rejected",
    "SchemaContractError",
    "ToolCatalog",
    "ToolCapability",
    "ToolExecutionContext",
    "ToolExecutionRecord",
    "ToolFailure",
    "ToolResultMetadata",
    "ToolSpec",
    "ToolSuccess",
    "UNAVAILABLE",
    "aggregate_binding",
    "canonical_json",
    "compile_tool_schema",
    "execute_prepared",
    "evaluate_context",
    "parse_arguments",
    "prepare_call",
    "project_transport_event",
    "render_compatibility",
    "validate_arguments",
]

_LAZY_PIPELINE_EXPORTS = frozenset({"Rejected", "execute_prepared", "prepare_call"})


def __getattr__(name: str) -> object:
    if name not in _LAZY_PIPELINE_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("offerpilot.ai.tool_runtime.pipeline"), name)
    globals()[name] = value
    return value
