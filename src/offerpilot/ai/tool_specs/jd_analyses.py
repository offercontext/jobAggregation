from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict, cast

from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.catalog import build_tool_spec
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    JSONValue,
    ToolSpec,
)
from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    ReadOperationMetadataV1,
    ResolverImplementationBinding,
    ToolPresentationBindingV1,
)
from offerpilot.ai.tool_runtime.policy_types import ToolCapability, ToolDomain
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


def _resolver(
    *,
    implementation_id: str,
    resolver_id: str,
    arg_path: str,
    presence: str,
    resolve: Any,
) -> ResolverImplementationBinding:
    descriptor = BindingResolverDescriptorV1(
        resolver_id=cast(Any, resolver_id),
        entity_kind="application",
        arg_path=arg_path,
        presence=cast(Any, presence),
        identity_type="positive_int64",
    )
    return ResolverImplementationBinding(
        descriptor=descriptor,
        implementation_id=implementation_id,
        resolve=resolve,
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


def _empty_confirmation_description(args: object) -> str:
    del args
    return ""


def _empty_pending_details(args: object, context: object | None = None) -> dict[str, object]:
    del args, context
    return {}


def jd_analysis_specs() -> tuple[ToolSpec[Any, Any], ...]:
    list_resolver = _resolver(
        implementation_id="list_jd_analyses_application_identity_arg_v1",
        resolver_id="application_identity_arg", arg_path="application_id",
        presence="optional", resolve=_application_binding,
    )
    get_resolver = _resolver(
        implementation_id="get_jd_analysis_jd_analysis_application_parent_v1",
        resolver_id="jd_analysis_application_parent", arg_path="id",
        presence="required", resolve=_analysis_binding,
    )
    return (
        build_tool_spec(
            contract=provider_contract("list_jd_analyses", "List saved JD analyses. Optionally filter by application id.", {"type": "object", "properties": {"application_id": {"type": "integer"}}}),
            domains=(ToolDomain.JD,), dependencies=(), required_capability=ToolCapability.JD_ANALYSES_READ,
            binding_contract=BindingContract("scoped_collection", "application"), resolver_bindings=(list_resolver,),
            confirmation_policy="none", editable_fields=(), operation=ReadOperationMetadataV1(),
            undo_builder_binding=None, presentation=ToolPresentationBindingV1(
                implementation_id="list_jd_analyses_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_list, success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract("get_jd_analysis", "Get one saved JD analysis by id.", {"type": "object", "properties": {"id": {"type": "integer", "description": "JD analysis id."}}, "required": ["id"]}),
            domains=(ToolDomain.JD,), dependencies=("list_jd_analyses",), required_capability=ToolCapability.JD_ANALYSES_READ,
            binding_contract=BindingContract("enforce_if_bound", "application"), resolver_bindings=(get_resolver,),
            confirmation_policy="none", editable_fields=(), operation=ReadOperationMetadataV1(),
            undo_builder_binding=None, presentation=ToolPresentationBindingV1(
                implementation_id="get_jd_analysis_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_get, declared_failure_categories=frozenset({"not_found"}),
            exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json,
        ),
    )
