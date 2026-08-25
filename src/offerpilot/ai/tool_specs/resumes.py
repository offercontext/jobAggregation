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
    EditableFieldMetadataV1,
    ReadOperationMetadataV1,
    ResolverImplementationBinding,
    ToolPresentationBindingV1,
    WriteOperationMetadataV1,
)
from offerpilot.ai.tool_runtime.policy_types import ToolCapability, ToolDomain
from offerpilot.ai.tool_specs.common import (
    INPUT_EXCEPTION_MAP,
    NOT_FOUND_EXCEPTION_MAP,
    ToolInputError,
    ToolRecordNotFound,
    compact_json,
    decode_mapping,
    integer,
    provider_contract,
    resume_json,
    resume_match_json,
    resolve_identity_argument,
)
from offerpilot.schemas import normalize_resume_content


class ResumeArgs(TypedDict, total=False):
    id: int
    resume_id: int
    career_intent: dict[str, Any]
    section: str
    item_index: int
    highlight_index: int
    text: str


def _decode(values: Mapping[str, JSONValue]) -> ResumeArgs:
    return cast(ResumeArgs, decode_mapping(values))


def _resume_id_binding(args: ResumeArgs, context: ToolExecutionContext) -> object:
    return resolve_identity_argument(
        args,
        context,
        entity_kind="resume",
        arg_path="id",
        presence="required",
    )


def _resume_match_binding(args: ResumeArgs, context: ToolExecutionContext) -> object:
    return resolve_identity_argument(
        args,
        context,
        entity_kind="resume",
        arg_path="resume_id",
        presence="required",
    )


def _resume_resolver(
    implementation_id: str,
    arg_path: str,
    resolve: Any,
) -> ResolverImplementationBinding:
    descriptor = BindingResolverDescriptorV1(
        resolver_id="resume_identity_arg",
        entity_kind="resume",
        arg_path=arg_path,
        presence="required",
        identity_type="positive_int64",
    )
    return ResolverImplementationBinding(
        descriptor=descriptor,
        implementation_id=implementation_id,
        resolve=resolve,
    )


def _list(args: ResumeArgs, context: ToolExecutionContext) -> list[dict[str, Any]]:
    del args
    return [resume_json(resume) for resume in context.resumes.list()]


def _get(args: ResumeArgs, context: ToolExecutionContext) -> dict[str, Any]:
    resume = context.resumes.get(integer(args, "id", "get_resume"))
    if resume is None:
        raise ToolRecordNotFound("resume not found")
    return resume_json(resume)


def _career(args: ResumeArgs, context: ToolExecutionContext) -> dict[str, Any]:
    resume_id = integer(args, "id", "resume_update_career_intent")
    career_intent = args.get("career_intent")
    if not isinstance(career_intent, dict):
        raise ToolInputError("resume_update_career_intent requires career_intent object")
    resume = context.resumes.get(resume_id)
    if resume is None or resume.deleted_at is not None:
        raise ToolRecordNotFound("resume not found")
    content = normalize_resume_content(resume.content_json)
    content["career_intent"] = career_intent
    updated = context.resumes.update(resume_id, {"content_json": content})
    if updated is None:
        raise ToolRecordNotFound("resume not found")
    return resume_json(updated)


def _highlight(args: ResumeArgs, context: ToolExecutionContext) -> dict[str, Any]:
    resume_id = integer(args, "id", "resume_rewrite_highlight")
    section = str(args.get("section") or "").strip()
    item_index = integer(args, "item_index", "resume_rewrite_highlight")
    highlight_index = integer(args, "highlight_index", "resume_rewrite_highlight")
    text = str(args.get("text") or "").strip()
    if not section:
        raise ToolInputError("resume_rewrite_highlight requires section")
    if item_index < 0:
        raise ToolInputError("item_index must be non-negative")
    if highlight_index < 0:
        raise ToolInputError("highlight_index must be non-negative")
    if not text:
        raise ToolInputError("resume_rewrite_highlight requires text")
    resume = context.resumes.get(resume_id)
    if resume is None or resume.deleted_at is not None:
        raise ToolRecordNotFound("resume not found")
    content = normalize_resume_content(resume.content_json)
    section_items = content.get(section)
    if not isinstance(section_items, list):
        raise ToolInputError(f"resume section not found: {section}")
    try:
        item = section_items[item_index]
    except IndexError as exc:
        raise ToolInputError("resume_rewrite_highlight item_index out of range") from exc
    if not isinstance(item, dict):
        raise ToolInputError("resume_rewrite_highlight item must be an object")
    highlights = item.get("highlights")
    if not isinstance(highlights, list):
        raise ToolInputError("resume_rewrite_highlight requires highlights list")
    try:
        highlights[highlight_index] = text
    except IndexError as exc:
        raise ToolInputError("resume_rewrite_highlight highlight_index out of range") from exc
    updated = context.resumes.update(resume_id, {"content_json": content})
    if updated is None:
        raise ToolRecordNotFound("resume not found")
    return resume_json(updated)


def _matches(args: ResumeArgs, context: ToolExecutionContext) -> list[dict[str, Any]]:
    resume_id = integer(args, "resume_id", "list_resume_matches")
    if context.resumes.get(resume_id) is None:
        raise ToolRecordNotFound("resume not found")
    return [resume_match_json(match) for match in context.resumes.list_matches(resume_id)]


def _empty_confirmation_description(args: object) -> str:
    del args
    return ""


def _empty_pending_details(args: object, context: object | None = None) -> dict[str, object]:
    del args, context
    return {}


def _describe_resume_update_career_intent(args: Mapping[str, Any]) -> str:
    return f"更新简历求职意向 #{args.get('id', '')}"


def _describe_resume_rewrite_highlight(args: Mapping[str, Any]) -> str:
    return f"改写简历亮点 #{args.get('id', '')}"


def resume_specs() -> tuple[ToolSpec[Any, Any], ...]:
    get_resolver = _resume_resolver(
        "get_resume_resume_identity_arg_v1", "id", _resume_id_binding
    )
    career_resolver = _resume_resolver(
        "resume_update_career_intent_resume_identity_arg_v1", "id", _resume_id_binding
    )
    highlight_resolver = _resume_resolver(
        "resume_rewrite_highlight_resume_identity_arg_v1", "id", _resume_id_binding
    )
    match_resolver = _resume_resolver(
        "list_resume_matches_resume_identity_arg_v1", "resume_id", _resume_match_binding
    )
    return (
        build_tool_spec(
            contract=provider_contract("list_resumes", "List resumes and their parse status.", {"type": "object", "properties": {}}),
            domains=(ToolDomain.RESUMES,), dependencies=(), required_capability=ToolCapability.RESUMES_READ,
            binding_contract=BindingContract("none"), resolver_bindings=(), confirmation_policy="none",
            editable_fields=(), operation=ReadOperationMetadataV1(), undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="list_resumes_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_list, success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract("get_resume", "Get one resume including parsed text by id.", {"type": "object", "properties": {"id": {"type": "integer", "description": "Resume id."}}, "required": ["id"]}),
            domains=(ToolDomain.RESUMES,), dependencies=("list_resumes",), required_capability=ToolCapability.RESUMES_READ,
            binding_contract=BindingContract("enforce_if_bound", "resume"), resolver_bindings=(get_resolver,),
            confirmation_policy="none", editable_fields=(), operation=ReadOperationMetadataV1(),
            undo_builder_binding=None, presentation=ToolPresentationBindingV1(
                implementation_id="get_resume_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_get, declared_failure_categories=frozenset({"not_found"}),
            exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract("resume_update_career_intent", "Update a resume's career_intent block. Requires user confirmation.", {"type": "object", "properties": {"id": {"type": "integer"}, "career_intent": {"type": "object"}}, "required": ["id", "career_intent"]}),
            domains=(ToolDomain.RESUMES,), dependencies=("get_resume",), required_capability=ToolCapability.RESUMES_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "resume"), resolver_bindings=(career_resolver,),
            confirmation_policy="required", editable_fields=(), operation=WriteOperationMetadataV1(),
            undo_builder_binding=None, presentation=ToolPresentationBindingV1(
                implementation_id="resume_update_career_intent_presentation_v1",
                confirmation_description=_describe_resume_update_career_intent,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_career,
            declared_failure_categories=frozenset({"validation_error", "not_found"}),
            exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract("resume_rewrite_highlight", "Rewrite one highlight in a structured resume section. Requires user confirmation.", {"type": "object", "properties": {"id": {"type": "integer"}, "section": {"type": "string"}, "item_index": {"type": "integer"}, "highlight_index": {"type": "integer"}, "text": {"type": "string"}}, "required": ["id", "section", "item_index", "highlight_index", "text"]}),
            domains=(ToolDomain.RESUMES,), dependencies=("get_resume",), required_capability=ToolCapability.RESUMES_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "resume"), resolver_bindings=(highlight_resolver,),
            confirmation_policy="required", editable_fields=(EditableFieldMetadataV1(
                field="text", value_type="long_text", options=None, clearable=False, clear_value=None
            ),), operation=WriteOperationMetadataV1(), undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="resume_rewrite_highlight_presentation_v1",
                confirmation_description=_describe_resume_rewrite_highlight,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_highlight,
            declared_failure_categories=frozenset({"validation_error", "not_found"}),
            exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract("list_resume_matches", "List saved JD match results for a resume.", {"type": "object", "properties": {"resume_id": {"type": "integer"}}, "required": ["resume_id"]}),
            domains=(ToolDomain.RESUMES,), dependencies=("list_resumes",), required_capability=ToolCapability.RESUMES_READ,
            binding_contract=BindingContract("enforce_if_bound", "resume"), resolver_bindings=(match_resolver,),
            confirmation_policy="none", editable_fields=(), operation=ReadOperationMetadataV1(),
            undo_builder_binding=None, presentation=ToolPresentationBindingV1(
                implementation_id="list_resume_matches_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_matches, declared_failure_categories=frozenset({"not_found"}),
            exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json,
        ),
    )
