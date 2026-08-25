from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict, cast

from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.policy_types import ToolCapability
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    BindingResolverSpec,
    JSONValue,
    ToolSpec,
)
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


_RESUME_ID_RESOLVER = BindingResolverSpec(
    resolver_id="resume_identity_arg",
    entity_kind="resume",
    arg_path="id",
    presence="required",
    identity_type="positive_int64",
    resolve=_resume_id_binding,
)
_RESUME_MATCH_RESOLVER = BindingResolverSpec(
    resolver_id="resume_identity_arg",
    entity_kind="resume",
    arg_path="resume_id",
    presence="required",
    identity_type="positive_int64",
    resolve=_resume_match_binding,
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


def resume_specs() -> tuple[ToolSpec[Any, Any], ...]:
    read = frozenset({ToolCapability.RESUMES_READ})
    write = frozenset({ToolCapability.RESUMES_WRITE})
    return (
        ToolSpec(contract=provider_contract("list_resumes", "List resumes and their parse status.", {"type": "object", "properties": {}}), kind="read", decoder=_decode, executor=_list, required_capabilities=read, binding_contract=BindingContract("none"), success_renderer=compact_json),
        ToolSpec(contract=provider_contract("get_resume", "Get one resume including parsed text by id.", {"type": "object", "properties": {"id": {"type": "integer", "description": "Resume id."}}, "required": ["id"]}), kind="read", decoder=_decode, executor=_get, required_capabilities=read, binding_contract=BindingContract("enforce_if_bound", "resume"), binding_resolvers=(_RESUME_ID_RESOLVER,), declared_failure_categories=frozenset({"not_found"}), exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json),
        ToolSpec(contract=provider_contract("resume_update_career_intent", "Update a resume's career_intent block. Requires user confirmation.", {"type": "object", "properties": {"id": {"type": "integer"}, "career_intent": {"type": "object"}}, "required": ["id", "career_intent"]}), kind="write", decoder=_decode, executor=_career, required_capabilities=write, binding_contract=BindingContract("enforce_if_bound", "resume"), binding_resolvers=(_RESUME_ID_RESOLVER,), confirmation_policy="required", declared_failure_categories=frozenset({"validation_error", "not_found"}), exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json),
        ToolSpec(contract=provider_contract("resume_rewrite_highlight", "Rewrite one highlight in a structured resume section. Requires user confirmation.", {"type": "object", "properties": {"id": {"type": "integer"}, "section": {"type": "string"}, "item_index": {"type": "integer"}, "highlight_index": {"type": "integer"}, "text": {"type": "string"}}, "required": ["id", "section", "item_index", "highlight_index", "text"]}), kind="write", decoder=_decode, executor=_highlight, required_capabilities=write, binding_contract=BindingContract("enforce_if_bound", "resume"), binding_resolvers=(_RESUME_ID_RESOLVER,), confirmation_policy="required", declared_failure_categories=frozenset({"validation_error", "not_found"}), exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json),
        ToolSpec(contract=provider_contract("list_resume_matches", "List saved JD match results for a resume.", {"type": "object", "properties": {"resume_id": {"type": "integer"}}, "required": ["resume_id"]}), kind="read", decoder=_decode, executor=_matches, required_capabilities=read, binding_contract=BindingContract("enforce_if_bound", "resume"), binding_resolvers=(_RESUME_MATCH_RESOLVER,), declared_failure_categories=frozenset({"not_found"}), exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json),
    )
