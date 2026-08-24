from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict, cast

from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    BindingResolverSpec,
    JSONValue,
    ToolSpec,
)
from offerpilot.ai.tool_specs.common import (
    NOT_FOUND_EXCEPTION_MAP,
    ToolRecordNotFound,
    compact_json,
    decode_mapping,
    integer,
    jd_analysis_json,
    provider_contract,
    resolve_identity_argument,
    resolve_parent_application,
)


class JDArgs(TypedDict, total=False):
    id: int
    application_id: int


def _decode(values: Mapping[str, JSONValue]) -> JDArgs:
    return cast(JDArgs, decode_mapping(values))


def _application_binding(args: JDArgs, context: ToolExecutionContext) -> object:
    return resolve_identity_argument(
        args,
        context,
        entity_kind="application",
        arg_path="application_id",
        presence="optional",
    )


def _analysis_binding(args: JDArgs, context: ToolExecutionContext) -> object:
    return resolve_parent_application(
        args,
        context,
        arg_path="id",
        entity_kind="application",
    )


_APPLICATION_OPTIONAL_RESOLVER = BindingResolverSpec(
    resolver_id="application_identity_arg",
    entity_kind="application",
    arg_path="application_id",
    presence="optional",
    identity_type="positive_int64",
    resolve=_application_binding,
)
_JD_ANALYSIS_PARENT_RESOLVER = BindingResolverSpec(
    resolver_id="jd_analysis_application_parent",
    entity_kind="application",
    arg_path="id",
    presence="required",
    identity_type="positive_int64",
    resolve=_analysis_binding,
)


def _list(args: JDArgs, context: ToolExecutionContext) -> list[dict[str, Any]]:
    return [
        jd_analysis_json(row)
        for row in context.jd_analyses.list_jd_analyses_scoped(
            context.scope_constraint,
            application_id=args.get("application_id"),
        )
    ]


def _get(args: JDArgs, context: ToolExecutionContext) -> dict[str, Any]:
    analysis = context.jd_analyses.get_jd_analysis_scoped(
        context.scope_constraint,
        integer(args, "id", "get_jd_analysis"),
    )
    if analysis is None:
        raise ToolRecordNotFound("jd analysis not found")
    return jd_analysis_json(analysis)


def jd_analysis_specs() -> tuple[ToolSpec[Any, Any], ...]:
    read = frozenset({ToolCapability.JD_ANALYSES_READ})
    return (
        ToolSpec(contract=provider_contract("list_jd_analyses", "List saved JD analyses. Optionally filter by application id.", {"type": "object", "properties": {"application_id": {"type": "integer"}}}), kind="read", decoder=_decode, executor=_list, required_capabilities=read, binding_contract=BindingContract("scoped_collection", "application"), binding_resolvers=(_APPLICATION_OPTIONAL_RESOLVER,), success_renderer=compact_json),
        ToolSpec(contract=provider_contract("get_jd_analysis", "Get one saved JD analysis by id.", {"type": "object", "properties": {"id": {"type": "integer", "description": "JD analysis id."}}, "required": ["id"]}), kind="read", decoder=_decode, executor=_get, required_capabilities=read, binding_contract=BindingContract("enforce_if_bound", "application"), binding_resolvers=(_JD_ANALYSIS_PARENT_RESOLVER,), declared_failure_categories=frozenset({"not_found"}), exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json),
    )
