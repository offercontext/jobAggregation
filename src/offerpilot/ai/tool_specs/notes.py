from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypedDict, cast

from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    BindingResolverSpec,
    JSONValue,
    ToolFailure,
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


_APPLICATION_OPTIONAL_RESOLVER = BindingResolverSpec(
    resolver_id="application_identity_arg",
    entity_kind="application",
    arg_path="application_id",
    presence="optional",
    identity_type="positive_int64",
    resolve=_application_binding,
)
_NOTE_PARENT_RESOLVER = BindingResolverSpec(
    resolver_id="note_application_parent",
    entity_kind="application",
    arg_path="id",
    presence="required",
    identity_type="positive_int64",
    resolve=_note_binding,
)


def _list(args: NoteArgs, context: ToolExecutionContext) -> list[dict[str, Any]]:
    return [note_json(note) for note in context.notes.list(application_id=optional_integer(args, "application_id"))]


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
    return ToolFailure("validation_error", "company_required", "add_note requires company when application_id is not provided")


def _add(args: NoteArgs, context: ToolExecutionContext) -> dict[str, Any]:
    application_id = optional_integer(args, "application_id") or None
    company = str(args.get("company") or "")
    position = str(args.get("position") or "")
    if application_id is not None:
        app = context.applications.get(application_id)
        if app is None:
            raise ToolRecordNotFound("application not found")
        company = company or app.company_name
        position = position or app.position_name
    if not company:
        raise ToolInputError("add_note requires company")
    note = context.notes.create(
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
        )
    )
    return note_json(note)


def _existing(args: NoteArgs, key: str, current: str) -> str:
    value = args.get(cast(Any, key))
    return current if value is None else str(value or "")


def _update(args: NoteArgs, context: ToolExecutionContext) -> dict[str, Any]:
    note_id = integer(args, "id", "update_note")
    existing = context.notes.get(note_id)
    if existing is None:
        raise ToolRecordNotFound("note not found")
    updated = context.notes.update(
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
    deleted = context.notes.get(note_id) is not None
    context.notes.delete(note_id)
    return {"deleted": deleted}


def _schema(required: list[JSONValue]) -> dict[str, JSONValue]:
    return {"type": "object", "properties": {"id": {"type": "integer"}, "application_id": {"type": "integer"}, "company": {"type": "string"}, "position": {"type": "string"}, "round": {"type": "string"}, "date": {"type": "string"}, "allow_placeholder_date": {"type": "boolean", "description": "Set true only after the user confirms saving an unclear interview date as 日期待定."}, "questions": {"type": "string"}, "self_reflection": {"type": "string"}, "difficulty_points": {"type": "string"}, "mood": {"type": "string"}}, "required": required}


def note_specs() -> tuple[ToolSpec[Any, Any], ...]:
    read = frozenset({ToolCapability.NOTES_READ})
    write = frozenset({ToolCapability.NOTES_WRITE})
    return (
        ToolSpec(contract=provider_contract("list_notes", "List interview review notes. Optionally filter by application id.", {"type": "object", "properties": {"application_id": {"type": "integer"}}}), kind="read", decoder=_decode, executor=_list, required_capabilities=read, binding_contract=BindingContract("scoped_collection", "application"), binding_resolvers=(_APPLICATION_OPTIONAL_RESOLVER,), success_renderer=compact_json),
        ToolSpec(contract=provider_contract("add_note", "Add an interview review note. If application_id is present, company and position can be omitted.", _schema([])), kind="write", decoder=_decode, executor=_add, required_capabilities=write, binding_contract=BindingContract("optional_target", "application"), binding_resolvers=(_APPLICATION_OPTIONAL_RESOLVER,), confirmation_policy="required", preflight=_validate_add, mutable_validator=_validate_add, declared_failure_categories=frozenset({"validation_error", "not_found"}), exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json),
        ToolSpec(contract=provider_contract("update_note", "Update an existing interview review note. Missing fields keep existing values.", _schema(["id"])), kind="write", decoder=_decode, executor=_update, required_capabilities=write, binding_contract=BindingContract("enforce_if_bound", "application"), binding_resolvers=(_NOTE_PARENT_RESOLVER, _APPLICATION_OPTIONAL_RESOLVER), confirmation_policy="required", declared_failure_categories=frozenset({"not_found"}), exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json),
        ToolSpec(contract=provider_contract("delete_note", "Delete an interview review note by id.", {"type": "object", "properties": {"id": {"type": "integer", "description": "Note id."}}, "required": ["id"]}), kind="write", decoder=_decode, executor=_delete, required_capabilities=write, binding_contract=BindingContract("enforce_if_bound", "application"), binding_resolvers=(_NOTE_PARENT_RESOLVER,), confirmation_policy="required", success_renderer=compact_json),
    )
