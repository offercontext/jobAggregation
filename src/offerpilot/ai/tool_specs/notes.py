from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypedDict, cast

from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.catalog import build_tool_spec
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    JSONValue,
    ToolExecutionRecord,
    ToolFailure,
    ToolSpec,
    ToolSuccess,
)
from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    EditableFieldMetadataV1,
    ReadOperationMetadataV1,
    ResolverImplementationBinding,
    ToolPresentationBindingV1,
    UndoBuilderBinding,
    WriteOperationMetadataV1,
)
from offerpilot.ai.tool_runtime.policy_types import (
    CompensationKind,
    ToolCapability,
    ToolDomain,
    UndoPayloadKind,
    UndoPolicy,
)
from offerpilot.ai.tool_specs.common import (
    INPUT_EXCEPTION_MAP,
    NOT_FOUND_EXCEPTION_MAP,
    ToolInputError,
    ToolRecordNotFound,
    compact_json,
    decode_mapping,
    integer,
    note_json,
    optional_integer,
    provider_contract,
    resolve_identity_argument,
    resolve_parent_application,
)
from offerpilot.repositories.notes import NoteCreate, NoteUpdate


class NoteArgs(TypedDict, total=False):
    id: int
    application_id: int
    company: str
    position: str
    round: str
    date: str
    allow_placeholder_date: bool
    questions: str
    self_reflection: str
    difficulty_points: str
    mood: str


def _decode(values: Mapping[str, JSONValue]) -> NoteArgs:
    return cast(NoteArgs, decode_mapping(values))


def _application_binding(args: NoteArgs, context: ToolExecutionContext) -> object:
    return resolve_identity_argument(
        args,
        context,
        entity_kind="application",
        arg_path="application_id",
        presence="optional",
    )


def _note_binding(args: NoteArgs, context: ToolExecutionContext) -> object:
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


def _list(args: NoteArgs, context: ToolExecutionContext) -> list[dict[str, Any]]:
    return [
        note_json(note)
        for note in context.notes.list_notes_scoped(
            context.scope_constraint,
            application_id=args.get("application_id"),
        )
    ]


def _validate_add(args: NoteArgs, context: ToolExecutionContext) -> ToolFailure | None:
    del context
    date = str(args.get("date") or "").strip()
    normalized = date.lower()
    unclear = normalized in {"日期待定", "待定", "unknown", "tbd"} or bool(
        re.search(r"(xx|x月|x日|某|待补|待确认|不详|未知)", normalized)
    )
    if date and not args.get("allow_placeholder_date") and unclear:
        return ToolFailure(
            "validation_error",
            "unclear_note_date",
            "add_note date is unclear; ask the user to provide a specific interview date or confirm saving it as 日期待定 before creating a pending confirmation.",
        )
    if optional_integer(args, "application_id") > 0 or str(args.get("company") or "").strip():
        return None
    return ToolFailure(
        "validation_error",
        "company_required",
        "add_note requires company when application_id is not provided",
    )


def _add(args: NoteArgs, context: ToolExecutionContext) -> dict[str, Any]:
    application_id = optional_integer(args, "application_id") or None
    company = str(args.get("company") or "")
    position = str(args.get("position") or "")
    if application_id is not None:
        app = context.applications.get_application_scoped(context.scope_constraint, application_id)
        if app is None:
            raise ToolRecordNotFound("application not found")
        company = company or app.company_name
        position = position or app.position_name
    if not company:
        raise ToolInputError("add_note requires company")
    note = context.notes.create_note_scoped(
        context.scope_constraint,
        NoteCreate(
            application_id=application_id,
            company=company,
            position=position,
            round=str(args.get("round") or ""),
            date=str(args.get("date") or ""),
            questions=str(args.get("questions") or ""),
            self_reflection=str(args.get("self_reflection") or ""),
            difficulty_points=str(args.get("difficulty_points") or ""),
            mood=str(args.get("mood") or ""),
        ),
    )
    return note_json(note)


def _existing(args: NoteArgs, key: str, current: str) -> str:
    value = args.get(cast(Any, key))
    return current if value is None else str(value or "")


def _update(args: NoteArgs, context: ToolExecutionContext) -> dict[str, Any]:
    note_id = integer(args, "id", "update_note")
    existing = context.notes.get_note_scoped(context.scope_constraint, note_id)
    if existing is None:
        raise ToolRecordNotFound("note not found")
    updated = context.notes.update_note_scoped(
        context.scope_constraint,
        note_id,
        NoteUpdate(
            application_id=existing.application_id,
            application_event_id=existing.application_event_id,
            company=_existing(args, "company", existing.company),
            position=_existing(args, "position", existing.position),
            round=_existing(args, "round", existing.round),
            date=_existing(args, "date", existing.date),
            questions=_existing(args, "questions", existing.questions),
            self_reflection=_existing(args, "self_reflection", existing.self_reflection),
            difficulty_points=_existing(args, "difficulty_points", existing.difficulty_points),
            mood=_existing(args, "mood", existing.mood),
        ),
    )
    if updated is None:
        raise ToolRecordNotFound("note not found")
    return note_json(updated)


def _delete(args: NoteArgs, context: ToolExecutionContext) -> dict[str, bool]:
    note_id = integer(args, "id", "delete_note")
    return {"deleted": context.notes.delete_note_scoped(context.scope_constraint, note_id)}


def _schema(required: list[JSONValue]) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "application_id": {"type": "integer"},
            "company": {"type": "string"},
            "position": {"type": "string"},
            "round": {"type": "string"},
            "date": {"type": "string"},
            "allow_placeholder_date": {
                "type": "boolean",
                "description": "Set true only after the user confirms saving an unclear interview date as 日期待定.",
            },
            "questions": {"type": "string"},
            "self_reflection": {"type": "string"},
            "difficulty_points": {"type": "string"},
            "mood": {"type": "string"},
        },
        "required": required,
    }


def _empty_confirmation_description(args: object) -> str:
    del args
    return ""


def _empty_pending_details(args: object, context: object | None = None) -> dict[str, object]:
    del args, context
    return {}


def _describe_add_note(args: Mapping[str, Any]) -> str:
    details = " · ".join(
        str(args.get(key) or "").strip()
        for key in ("company", "position", "round")
        if str(args.get(key) or "").strip()
    )
    return f"新增复盘：{details}" if details else "新增复盘"


def _describe_update_note(args: Mapping[str, Any]) -> str:
    return f"更新复盘 #{args.get('id', '')}"


def _describe_delete_note(args: Mapping[str, Any]) -> str:
    return f"删除复盘 #{args.get('id', '')}"


def _project_add_note_success(result: object) -> str:
    if not isinstance(result, Mapping):
        return ""
    record_id = result.get("note_id") or result.get("id")
    company = str(result.get("company") or "").strip()
    position = str(result.get("position") or "").strip()
    round_name = str(result.get("round") or "").strip()
    meta = " · ".join(value for value in (company, position, round_name) if value)
    return f"✅ 保存成功：复盘记录 #{record_id} 已保存（{meta}）。" if record_id and meta else ""


def _short_preview(value: str, max_length: int = 180) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."


def _pending_note_draft_summary(changes: list[dict[str, Any]]) -> dict[str, Any]:
    labels = {
        "questions": "问题记录",
        "self_reflection": "自我复盘",
        "difficulty_points": "难点短板",
        "mood": "感受",
        "notes": "备注",
    }
    fields = []
    for change in changes:
        field = str(change.get("field") or "")
        after = change.get("after")
        if field not in labels or not isinstance(after, str):
            continue
        normalized = " ".join(after.split())
        if len(normalized) < 80:
            continue
        fields.append(
            {
                "field": field,
                "label": labels[field],
                "summary": _short_preview(after, 96),
                "characters": len(normalized),
            }
        )
    return {"title": "复盘草稿", "fields": fields} if fields else {}


def _pending_add_note(args: Mapping[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    company = str(args.get("company") or "").strip()
    position = str(args.get("position") or "").strip()
    application = None
    application_id = args.get("application_id")
    if isinstance(application_id, (int, str)) and str(application_id).strip():
        try:
            application = context.applications.get(int(application_id))
        except ValueError:
            application = None
    if application is not None:
        company = company or application.company_name
        position = position or application.position_name
    round_name = str(args.get("round") or "").strip()
    date = str(args.get("date") or "").strip()
    title = company or "公司待补充"
    target: dict[str, Any] = {
        "id": f"note-draft-{title}-{position or 'unknown'}",
        "kind": "note",
        "title": title,
        "meta": " · ".join(value for value in (position, round_name, date) if value),
        "source": "pending_action",
    }
    questions = str(args.get("questions") or "").strip()
    if questions:
        target["snippet"] = _short_preview(questions)
    proposed_changes = [
        {"field": key, "before": "", "after": value}
        for key, value in (
            ("company", company),
            ("position", position),
            ("round", round_name),
            ("date", date),
            ("questions", questions),
            ("self_reflection", str(args.get("self_reflection") or "").strip()),
            ("difficulty_points", str(args.get("difficulty_points") or "").strip()),
            ("mood", str(args.get("mood") or "").strip()),
        )
        if value
    ]
    evidence = []
    if application is not None:
        evidence.append(
            {
                "id": f"application-{application.id}",
                "kind": "application",
                "title": application.company_name,
                "meta": " · ".join(
                    value for value in (application.position_name, application.status) if value
                ),
                "source": "pending_action",
            }
        )
    details: dict[str, Any] = {
        "target": target,
        "proposed_changes": proposed_changes,
        "evidence": evidence,
        "risk_hint": "基于本轮对话整理，请确认结构化内容无误。",
        "workflow": {
            "current_step": 2,
            "total_steps": 2,
            "current_label": "保存面试复盘",
            "description": "这是本次连续写入的最后一步。",
        },
    }
    draft_summary = _pending_note_draft_summary(proposed_changes)
    if draft_summary:
        details["draft_summary"] = draft_summary
    return details


def _capture_no_undo_seed(context: object, args: object) -> None:
    del context, args
    return None


def _build_add_note_undo(seed: object, record: ToolExecutionRecord[Any, Any]) -> dict[str, Any]:
    del seed
    if not isinstance(record.outcome, ToolSuccess) or not isinstance(record.outcome.result, dict):
        return {}
    payload = cast(dict[str, Any], record.outcome.result)
    note_id = payload.get("note_id") or payload.get("id")
    try:
        resolved_id = int(str(note_id))
    except (TypeError, ValueError):
        return {}
    fields = (
        "application_id",
        "company",
        "position",
        "round",
        "date",
        "questions",
        "self_reflection",
        "difficulty_points",
        "mood",
    )
    return {
        "kind": "delete_note",
        "label": "撤销保存复盘",
        "note_id": resolved_id,
        "expected_after": {field: payload.get(field) for field in fields},
    }


def _editable(field: str, value_type: str) -> EditableFieldMetadataV1:
    return EditableFieldMetadataV1(
        field=field,
        value_type=cast(Any, value_type),
        options=None,
        clearable=False,
        clear_value=None,
    )


def note_specs() -> tuple[ToolSpec[Any, Any], ...]:
    note_fields = tuple(
        _editable(field, value_type)
        for field, value_type in (
            ("company", "string"),
            ("position", "string"),
            ("round", "string"),
            ("date", "datetime"),
            ("allow_placeholder_date", "boolean"),
            ("questions", "long_text"),
            ("self_reflection", "long_text"),
            ("difficulty_points", "long_text"),
            ("mood", "long_text"),
        )
    )
    add_operation = WriteOperationMetadataV1(
        undo_policy=UndoPolicy.REQUIRED,
        undo_payload_kind=UndoPayloadKind.DELETE_NOTE,
        compensation_kind=CompensationKind.UNDO_ADD_NOTE,
        undo_contract_version="write-undo-payload-v1",
        undo_builder_id="add_note_delete_v1",
        undo_seed_phase="none",
    )
    add_undo = UndoBuilderBinding(
        descriptor=add_operation,
        implementation_id="add_note_delete_v1",
        capture_seed=_capture_no_undo_seed,
        build_undo=_build_add_note_undo,
    )
    list_resolver = _resolver(
        implementation_id="list_notes_application_identity_arg_v1",
        resolver_id="application_identity_arg",
        arg_path="application_id",
        presence="optional",
        resolve=_application_binding,
    )
    add_resolver = _resolver(
        implementation_id="add_note_application_identity_arg_v1",
        resolver_id="application_identity_arg",
        arg_path="application_id",
        presence="optional",
        resolve=_application_binding,
    )
    update_parent = _resolver(
        implementation_id="update_note_note_application_parent_v1",
        resolver_id="note_application_parent",
        arg_path="id",
        presence="required",
        resolve=_note_binding,
    )
    update_application = _resolver(
        implementation_id="update_note_application_identity_arg_v1",
        resolver_id="application_identity_arg",
        arg_path="application_id",
        presence="optional",
        resolve=_application_binding,
    )
    delete_resolver = _resolver(
        implementation_id="delete_note_note_application_parent_v1",
        resolver_id="note_application_parent",
        arg_path="id",
        presence="required",
        resolve=_note_binding,
    )
    return (
        build_tool_spec(
            contract=provider_contract(
                "list_notes",
                "List interview review notes. Optionally filter by application id.",
                {"type": "object", "properties": {"application_id": {"type": "integer"}}},
            ),
            domains=(ToolDomain.NOTES,),
            dependencies=(),
            required_capability=ToolCapability.NOTES_READ,
            binding_contract=BindingContract("scoped_collection", "application"),
            resolver_bindings=(list_resolver,),
            confirmation_policy="none",
            editable_fields=(),
            operation=ReadOperationMetadataV1(),
            undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="list_notes_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ),
            decoder=_decode,
            executor=_list,
            success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract(
                "add_note",
                "Add an interview review note. If application_id is present, company and position can be omitted.",
                _schema([]),
            ),
            domains=(ToolDomain.NOTES,),
            dependencies=(),
            required_capability=ToolCapability.NOTES_WRITE,
            binding_contract=BindingContract("optional_target", "application"),
            resolver_bindings=(add_resolver,),
            confirmation_policy="required",
            editable_fields=note_fields,
            operation=add_operation,
            undo_builder_binding=add_undo,
            presentation=ToolPresentationBindingV1(
                implementation_id="add_note_presentation_v1",
                confirmation_description=_describe_add_note,
                pending_details_projector=_pending_add_note,
                success_summary_projector=_project_add_note_success,
            ),
            decoder=_decode,
            executor=_add,
            preflight=_validate_add,
            mutable_validator=_validate_add,
            declared_failure_categories=frozenset({"validation_error", "not_found"}),
            exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP,
            success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract(
                "update_note",
                "Update an existing interview review note. Missing fields keep existing values.",
                _schema(["id"]),
            ),
            domains=(ToolDomain.NOTES,),
            dependencies=("list_notes",),
            required_capability=ToolCapability.NOTES_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            resolver_bindings=(update_parent, update_application),
            confirmation_policy="required",
            editable_fields=note_fields,
            operation=WriteOperationMetadataV1(),
            undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="update_note_presentation_v1",
                confirmation_description=_describe_update_note,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ),
            decoder=_decode,
            executor=_update,
            declared_failure_categories=frozenset({"not_found"}),
            exception_map=NOT_FOUND_EXCEPTION_MAP,
            success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract(
                "delete_note",
                "Delete an interview review note by id.",
                {
                    "type": "object",
                    "properties": {"id": {"type": "integer", "description": "Note id."}},
                    "required": ["id"],
                },
            ),
            domains=(ToolDomain.NOTES,),
            dependencies=("list_notes",),
            required_capability=ToolCapability.NOTES_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            resolver_bindings=(delete_resolver,),
            confirmation_policy="required",
            editable_fields=(),
            operation=WriteOperationMetadataV1(),
            undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="delete_note_presentation_v1",
                confirmation_description=_describe_delete_note,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ),
            decoder=_decode,
            executor=_delete,
            success_renderer=compact_json,
        ),
    )
