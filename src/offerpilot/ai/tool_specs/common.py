from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, cast

from offerpilot.ai.tool_authority.contracts import require_positive_int64
from offerpilot.ai.tool_runtime.contracts import (
    JSONValue,
    ProviderToolContract,
    ToolExceptionMapping,
)
from offerpilot.repositories.application_events import duration_minutes
from offerpilot.schemas import (
    ApplicationEventOut,
    ApplicationOut,
    InterviewNoteOut,
    JDAnalysisOut,
    OfferOut,
    ResumeMatchOut,
    resume_payload,
)


class ToolInputError(Exception):
    """A declared domain input failure whose text is part of the legacy contract."""


class ToolRecordNotFound(Exception):
    """A declared missing-record failure."""


class ToolStateConflict(Exception):
    """A declared mutable-state conflict."""


BindingResolutionState = Literal["resolved", "omitted", "detached", "unavailable"]


class _AuthorityBoundResolverContext(Protocol):
    def binding_target_resolution(
        self,
        *,
        entity_kind: Literal["application", "resume"],
        state: BindingResolutionState,
        identity: int | None,
    ) -> object: ...

    def resolve_parent_identity(
        self,
        entity_kind: str,
        identity: int,
    ) -> tuple[str, int | None]: ...


def _authority_binding_resolution(
    context: object,
    *,
    entity_kind: Literal["application", "resume"],
    state: BindingResolutionState,
    identity: int | None,
) -> object:
    """Return a resolution through the exact authority-bound context port."""

    if state == "resolved":
        require_positive_int64(identity, "binding identity")
    elif identity is not None:
        raise ValueError("only resolved binding targets may carry identity")

    resolver_context = cast(_AuthorityBoundResolverContext, context)
    return resolver_context.binding_target_resolution(
        entity_kind=entity_kind,
        state=state,
        identity=identity,
    )


def resolve_identity_argument(
    args: Mapping[str, object],
    context: object,
    *,
    entity_kind: Literal["application", "resume"],
    arg_path: str,
    presence: Literal["required", "optional"],
) -> object:
    """Resolve a primitive typed-args identity without coercion."""

    if arg_path not in args:
        state: BindingResolutionState = "omitted" if presence == "optional" else "unavailable"
        return _authority_binding_resolution(
            context, entity_kind=entity_kind, state=state, identity=None
        )
    raw = args[arg_path]
    if type(raw) is not int or not 1 <= raw <= 2**63 - 1:
        return _authority_binding_resolution(
            context, entity_kind=entity_kind, state="unavailable", identity=None
        )
    return _authority_binding_resolution(
        context, entity_kind=entity_kind, state="resolved", identity=raw
    )


def resolve_parent_application(
    args: Mapping[str, object],
    context: object,
    *,
    arg_path: str,
    entity_kind: Literal["application"],
) -> object:
    """Resolve only a primitive parent identity through an explicit context port.

    The port returns ``(state, parent_id)`` or an object/mapping with the same
    fields.  A missing port is unavailable; no fallback to Repository.get()
    is allowed because that would read and retain entity bodies in a resolver.
    """

    identity = args.get(arg_path)
    if type(identity) is not int or not 1 <= identity <= 2**63 - 1:
        return _authority_binding_resolution(
            context, entity_kind=entity_kind, state="unavailable", identity=None
        )
    resolver_context = cast(_AuthorityBoundResolverContext, context)
    result = resolver_context.resolve_parent_identity(entity_kind, identity)
    if isinstance(result, Mapping):
        state = result.get("state")
        parent_id = result.get("identity")
    elif isinstance(result, tuple) and len(result) == 2:
        state, parent_id = result
    else:
        state = getattr(result, "state", None)
        parent_id = getattr(result, "identity", None)
    if state not in {"resolved", "detached", "unavailable"}:
        state = "unavailable"
        parent_id = None
    if state == "resolved" and (type(parent_id) is not int or not 1 <= parent_id <= 2**63 - 1):
        state = "unavailable"
        parent_id = None
    return _authority_binding_resolution(
        context,
        entity_kind=entity_kind,
        state=cast(BindingResolutionState, state),
        identity=parent_id,
    )


INPUT_EXCEPTION_MAP = (
    ToolExceptionMapping(ToolInputError, "validation_error", "domain_validation", str),
)
NOT_FOUND_EXCEPTION_MAP = (
    ToolExceptionMapping(ToolRecordNotFound, "not_found", "record_not_found", str),
)
CONFLICT_EXCEPTION_MAP = (
    ToolExceptionMapping(ToolStateConflict, "conflict", "state_conflict", str),
)


def provider_contract(
    name: str,
    description: str,
    parameters: Mapping[str, JSONValue],
) -> ProviderToolContract:
    function: dict[str, JSONValue] = {
        "name": name,
        "description": description,
        "parameters": dict(parameters),
    }
    payload: dict[str, JSONValue] = {"type": "function", "function": function}
    return ProviderToolContract(
        payload=payload,
        name=name,
        description=description,
        parameters=parameters,
    )


def decode_mapping(values: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    return dict(values)


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def spaced_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def application_json(app: Any) -> dict[str, Any]:
    payload = ApplicationOut.model_validate(app).model_dump(mode="json")
    payload["record_type"] = "application"
    payload["application_id"] = app.id
    return payload


def event_json(event: Any) -> dict[str, Any]:
    payload = ApplicationEventOut(
        id=event.id,
        application_id=event.application_id,
        event_type=event.event_type,
        subtype=event.subtype,
        tags=event.tags,
        round=event.round,
        scheduled_at=format_rfc3339(event.scheduled_at),
        duration_minutes=duration_minutes(event.duration_minutes),
        location=event.location,
        notes=event.notes,
        remind_at=format_rfc3339(event.remind_at) if event.remind_at else None,
        status=event.status,
        created_at=event.created_at,
    ).model_dump(mode="json", exclude_none=True)
    payload["record_type"] = "application_event"
    payload["application_event_id"] = event.id
    return payload


def event_with_application_json(item: Any) -> dict[str, Any]:
    payload = event_json(item.event)
    payload["company_name"] = item.company_name
    payload["position_name"] = item.position_name
    return payload


def note_json(note: Any) -> dict[str, Any]:
    payload = InterviewNoteOut.model_validate(note).model_dump(mode="json", exclude_none=False)
    payload["record_type"] = "note"
    payload["note_id"] = note.id
    return payload


def offer_json(offer: Any) -> dict[str, Any]:
    payload = OfferOut.model_validate(offer).model_dump(mode="json", exclude_none=False)
    payload["record_type"] = "offer"
    payload["offer_id"] = offer.id
    return payload


def resume_json(resume: Any) -> dict[str, Any]:
    payload = resume_payload(resume)
    payload["record_type"] = "resume"
    payload["resume_id"] = resume.id
    return payload


def resume_match_json(match: Any) -> dict[str, Any]:
    payload = ResumeMatchOut.model_validate(match).model_dump(mode="json", exclude_none=False)
    payload["record_type"] = "resume_match"
    payload["resume_match_id"] = match.id
    return payload


def jd_analysis_json(analysis: Any) -> dict[str, Any]:
    payload = JDAnalysisOut.model_validate(analysis).model_dump(mode="json", exclude_none=False)
    payload["record_type"] = "jd_analysis"
    payload["jd_analysis_id"] = analysis.id
    return payload


def format_rfc3339(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def integer(args: Mapping[str, object], key: str, tool_name: str) -> int:
    raw = args.get(key)
    if raw is None or raw == "":
        raise ToolInputError(f"{tool_name} requires {key}")
    try:
        return int(cast(Any, raw))
    except (TypeError, ValueError) as exc:
        raise ToolInputError(f"{tool_name} requires numeric {key}") from exc


def optional_integer(args: Mapping[str, object], key: str) -> int:
    raw = args.get(key)
    if raw is None or raw == "":
        return 0
    try:
        return int(cast(Any, raw))
    except (TypeError, ValueError) as exc:
        raise ToolInputError(f"{key} must be numeric") from exc
