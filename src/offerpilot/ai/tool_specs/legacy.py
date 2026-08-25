from __future__ import annotations

import json
from datetime import datetime
from typing import Any, TYPE_CHECKING, cast

from offerpilot.ai.tool_runtime.contracts import JSONValue
from offerpilot.ai.tool_runtime.legacy import (
    LegacyDeterministicAdapter,
    LegacyDeterministicAdapterSpec,
    LegacyDeterministicCatalog,
    LegacyExecutionContextPort,
    LegacyPresentationBindingV1,
    LegacyReadContextPort,
    LegacyRouteSourceV1,
    LegacyStaticAdapterCatalogV1,
)
from offerpilot.ai.tool_runtime.metadata import FrozenJSONObject, freeze_json
from offerpilot.ai.tool_runtime.protocol_seals import verify_legacy_boundary
from offerpilot.repositories.application_jd_versions import (
    IDEMPOTENCY_KEY_RE,
    ApplicationJDService,
    JDVersionError,
)
from offerpilot.repositories.application_outcomes import (
    FEEDBACK_TAGS,
    RESULTS,
    STAGES,
    ApplicationOutcomeError,
    ApplicationOutcomesRepository,
    OutcomeCreate,
    SubmissionSnapshotCreate,
)


if TYPE_CHECKING:
    from offerpilot.pilot_runtime.contracts import LegacyExecutionContext, LegacyReadContext


def _payload(args: str) -> dict[str, Any]:
    if not args:
        return {}
    value = json.loads(args)
    if not isinstance(value, dict):
        raise ValueError("tool args must be an object")
    return value


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_iso_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO datetime")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed


def _validate_jd(args: str) -> str:
    try:
        payload = json.loads(args)
    except json.JSONDecodeError:
        return "save_application_jd_version requires a JSON object"
    if not isinstance(payload, dict):
        return "save_application_jd_version requires a JSON object"
    try:
        application_id = payload.get("application_id")
        if type(application_id) is not int or application_id <= 0:
            return "application_id must be a positive integer"
        if not isinstance(payload.get("jd_text"), str) or not payload["jd_text"].strip():
            return "jd_text is required"
        expected = payload.get("expected_current_version_id")
        if expected is not None and (type(expected) is not int or expected <= 0):
            return "expected_current_version_id must be a positive integer or null"
        key = payload.get("idempotency_key")
        if not isinstance(key, str) or IDEMPOTENCY_KEY_RE.fullmatch(key) is None:
            return "idempotency_key is invalid"
    except (KeyError, TypeError):
        return "invalid application JD payload"
    return ""


def _describe_jd(args: str) -> str:
    try:
        payload = json.loads(args)
    except json.JSONDecodeError:
        return "Save the supplied job description after confirmation."
    app_id = payload.get("application_id") if isinstance(payload, dict) else None
    return f"Confirm saving the job description to application {app_id}. The source URL will not be opened."


def _describe_snapshot(args: str) -> str:
    return f"冻结投递 #{_payload(args).get('application_id')} 的实际简历、JD 和材料。"


def _describe_outcome(args: str) -> str:
    payload = _payload(args)
    return (
        f"记录投递 #{payload.get('application_id')} 的 "
        f"{payload.get('stage')} / {payload.get('result')} 结果。"
    )


def _execute_jd(service: ApplicationJDService, args: str) -> str:
    payload = json.loads(args)
    try:
        result = service.create_version(
            int(payload["application_id"]),
            jd_text=payload["jd_text"],
            source_url=payload.get("source_url"),
            source_kind="pilot",
            expected_current_version_id=payload.get("expected_current_version_id"),
            idempotency_key=payload["idempotency_key"],
        )
    except JDVersionError as exc:
        raise ValueError(exc.code) from exc
    return _json(
        {
            "record_type": "application_jd_version",
            "id": result.version.id,
            "application_id": result.version.application_id,
            "version_number": result.version.version_number,
            "source_kind": result.version.source_kind,
            "replayed": result.replayed,
        }
    )


def _validate_snapshot(args: str) -> str:
    try:
        payload = _payload(args)
        for field in ("application_id", "resume_id", "jd_version_id"):
            if type(payload.get(field)) is not int or payload[field] <= 0:
                return f"{field} must be a positive integer"
        material_id = payload.get("material_kit_id")
        if material_id is not None and (type(material_id) is not int or material_id <= 0):
            return "material_kit_id must be a positive integer or null"
        _parse_iso_datetime(payload.get("submitted_at"), "submitted_at")
        key = payload.get("idempotency_key")
        if not isinstance(key, str) or IDEMPOTENCY_KEY_RE.fullmatch(key) is None:
            return "idempotency_key is invalid"
        if not isinstance(payload.get("note"), str):
            return "note must be a string"
    except (KeyError, TypeError, ValueError):
        return "invalid application submission snapshot payload"
    return ""


def _validate_outcome(args: str) -> str:
    try:
        payload = _payload(args)
        for field in ("application_id", "submission_snapshot_id"):
            if type(payload.get(field)) is not int or payload[field] <= 0:
                return f"{field} must be a positive integer"
        event_id = payload.get("application_event_id")
        if event_id is not None and (type(event_id) is not int or event_id <= 0):
            return "application_event_id must be a positive integer or null"
        if payload.get("stage") not in STAGES:
            return "stage is invalid"
        if payload.get("result") not in RESULTS:
            return "result is invalid"
        tags = payload.get("feedback_tags")
        if (
            not isinstance(tags, list)
            or len(tags) > 8
            or any(tag not in FEEDBACK_TAGS for tag in tags)
        ):
            return "feedback_tags are invalid"
        for field in ("feedback_text", "reflection_text", "next_action_text"):
            if not isinstance(payload.get(field), str):
                return f"{field} must be a string"
        _parse_iso_datetime(payload.get("occurred_at"), "occurred_at")
        key = payload.get("idempotency_key")
        if not isinstance(key, str) or IDEMPOTENCY_KEY_RE.fullmatch(key) is None:
            return "idempotency_key is invalid"
    except (KeyError, TypeError, ValueError):
        return "invalid application outcome payload"
    return ""


def _execute_snapshot(repo: ApplicationOutcomesRepository, args: str) -> str:
    payload = _payload(args)
    try:
        result = repo.create_snapshot(
            SubmissionSnapshotCreate(
                application_id=payload["application_id"],
                resume_id=payload["resume_id"],
                jd_version_id=payload["jd_version_id"],
                material_kit_id=payload.get("material_kit_id"),
                submitted_at=_parse_iso_datetime(payload["submitted_at"], "submitted_at"),
                note=payload["note"],
                source_kind="pilot",
                idempotency_key=payload["idempotency_key"],
            )
        )
    except ApplicationOutcomeError as exc:
        raise ValueError(exc.code) from exc
    return _json(
        {
            "record_type": "application_submission_snapshot",
            "id": result.value.id,
            "application_id": result.value.application_id,
            "replayed": result.replayed,
        }
    )


def _execute_outcome(repo: ApplicationOutcomesRepository, args: str) -> str:
    payload = _payload(args)
    try:
        result = repo.create_outcome(
            OutcomeCreate(
                application_id=payload["application_id"],
                submission_snapshot_id=payload["submission_snapshot_id"],
                application_event_id=payload.get("application_event_id"),
                stage=payload["stage"],
                result=payload["result"],
                feedback_text=payload["feedback_text"],
                reflection_text=payload["reflection_text"],
                next_action_text=payload["next_action_text"],
                feedback_tags=tuple(payload["feedback_tags"]),
                occurred_at=_parse_iso_datetime(payload["occurred_at"], "occurred_at"),
                source_kind="pilot",
                idempotency_key=payload["idempotency_key"],
            )
        )
    except ApplicationOutcomeError as exc:
        raise ValueError(exc.code) from exc
    return _json(
        {
            "record_type": "application_outcome",
            "id": result.value.id,
            "application_id": result.value.application_id,
            "replayed": result.replayed,
        }
    )


def _require_exact_execution_context(
    context: LegacyExecutionContextPort,
) -> LegacyExecutionContext:
    from offerpilot.pilot_runtime.contracts import LegacyExecutionContext

    if type(context) is not LegacyExecutionContext:
        raise TypeError("Legacy execution context must be the exact sealed capability")
    context.require_integrity()
    return context


def _require_exact_read_context(context: LegacyReadContextPort) -> LegacyReadContext:
    from offerpilot.pilot_runtime.contracts import LegacyReadContext

    if type(context) is not LegacyReadContext:
        raise TypeError("Legacy read context must be the exact sealed capability")
    context.require_integrity()
    return context


def _project_jd_pending_details(
    encoded_args: str,
    context: LegacyReadContextPort,
) -> FrozenJSONObject:
    exact_context = _require_exact_read_context(context)
    try:
        payload = _payload(encoded_args)
    except (TypeError, ValueError):
        return freeze_json({})
    application_id = payload.get("application_id")
    if type(application_id) is not int or application_id <= 0:
        return freeze_json({})
    application = exact_context.application_summary(application_id)
    if application is None:
        return freeze_json({})
    target = {
        "id": f"application-{application['id']}",
        "kind": "application",
        "title": application["company_name"],
        "meta": application["position_name"],
        "source": "pending_action",
    }
    expected_version_id = payload.get("expected_current_version_id")
    current_number = (
        exact_context.jd_version_number(application_id, expected_version_id)
        if type(expected_version_id) is int
        else None
    )
    return freeze_json(
        {
            "target": target,
            "evidence": [target],
            "application_jd": {
                "current_version_number": current_number,
                "proposed_version_number": (current_number or 0) + 1,
            },
        }
    )


def _project_empty_legacy_pending_details(
    _encoded_args: str,
    context: LegacyReadContextPort,
) -> FrozenJSONObject:
    _require_exact_read_context(context)
    return freeze_json({})


def _project_legacy_success_summary(
    _encoded_result: str,
    context: LegacyReadContextPort,
) -> str:
    _require_exact_read_context(context)
    return "岗位资料已保存。"


def _execute_static_jd(
    encoded_args: str,
    context: LegacyExecutionContextPort,
) -> str:
    exact_context = _require_exact_execution_context(context)
    return _execute_jd(exact_context.jd_service, encoded_args)


def _execute_static_snapshot(
    encoded_args: str,
    context: LegacyExecutionContextPort,
) -> str:
    exact_context = _require_exact_execution_context(context)
    return _execute_snapshot(exact_context.outcomes_repository, encoded_args)


def _execute_static_outcome(
    encoded_args: str,
    context: LegacyExecutionContextPort,
) -> str:
    exact_context = _require_exact_execution_context(context)
    return _execute_outcome(exact_context.outcomes_repository, encoded_args)


def build_static_legacy_adapter_catalog() -> LegacyStaticAdapterCatalogV1:
    """Build the unpublished static three-Adapter catalog as one sealed unit."""

    catalog = LegacyStaticAdapterCatalogV1(
        (
            LegacyDeterministicAdapterSpec(
                ordinal=1,
                name="save_application_jd_version",
                editable_fields=(
                    {"field": "jd_text", "type": "long_text"},
                    {
                        "field": "source_url",
                        "type": "string",
                        "clearable": True,
                        "clear_value": None,
                    },
                ),
                chained_policy="same_adapter_only",
                initial_route_sources=(
                    LegacyRouteSourceV1("jd_clarification"),
                    LegacyRouteSourceV1("jd_deterministic_action"),
                ),
                describe=_describe_jd,
                validate=_validate_jd,
                presentation=LegacyPresentationBindingV1(
                    implementation_id="legacy-presentation-save-application-jd-version-v1",
                    confirmation_description=_describe_jd,
                    pending_details_projector=_project_jd_pending_details,
                    success_summary_projector=_project_legacy_success_summary,
                ),
                execute=_execute_static_jd,
            ),
            LegacyDeterministicAdapterSpec(
                ordinal=2,
                name="create_application_submission_snapshot",
                editable_fields=(
                    {"field": "submitted_at", "type": "datetime"},
                    {"field": "note", "type": "long_text"},
                ),
                chained_policy="forbidden",
                initial_route_sources=(LegacyRouteSourceV1("submission_snapshot_action"),),
                describe=_describe_snapshot,
                validate=_validate_snapshot,
                presentation=LegacyPresentationBindingV1(
                    implementation_id=(
                        "legacy-presentation-create-application-submission-snapshot-v1"
                    ),
                    confirmation_description=_describe_snapshot,
                    pending_details_projector=_project_empty_legacy_pending_details,
                    success_summary_projector=_project_legacy_success_summary,
                ),
                execute=_execute_static_snapshot,
            ),
            LegacyDeterministicAdapterSpec(
                ordinal=3,
                name="record_application_outcome",
                editable_fields=(
                    {
                        "field": "stage",
                        "type": "enum",
                        "options": cast(JSONValue, sorted(STAGES)),
                    },
                    {
                        "field": "result",
                        "type": "enum",
                        "options": cast(JSONValue, sorted(RESULTS)),
                    },
                    {"field": "feedback_text", "type": "long_text"},
                    {"field": "reflection_text", "type": "long_text"},
                    {"field": "next_action_text", "type": "long_text"},
                    {"field": "occurred_at", "type": "datetime"},
                ),
                chained_policy="forbidden",
                initial_route_sources=(LegacyRouteSourceV1("outcome_recording_action"),),
                describe=_describe_outcome,
                validate=_validate_outcome,
                presentation=LegacyPresentationBindingV1(
                    implementation_id="legacy-presentation-record-application-outcome-v1",
                    confirmation_description=_describe_outcome,
                    pending_details_projector=_project_empty_legacy_pending_details,
                    success_summary_projector=_project_legacy_success_summary,
                ),
                execute=_execute_static_outcome,
            ),
        )
    )
    verify_legacy_boundary(
        tuple(adapter.name for adapter in catalog.ordered_adapters),
        "forbidden",
        "legacy_deterministic",
    )
    return catalog


def build_legacy_deterministic_catalog(
    jd_service: ApplicationJDService,
    outcomes: ApplicationOutcomesRepository,
) -> LegacyDeterministicCatalog:
    return LegacyDeterministicCatalog(
        (
            LegacyDeterministicAdapter(
                name="save_application_jd_version",
                editable_fields=(
                    {"field": "jd_text", "type": "long_text"},
                    {
                        "field": "source_url",
                        "type": "string",
                        "clearable": True,
                        "clear_value": None,
                    },
                ),
                describe=_describe_jd,
                validate=_validate_jd,
                execute=lambda args: _execute_jd(jd_service, args),
            ),
            LegacyDeterministicAdapter(
                name="create_application_submission_snapshot",
                editable_fields=(
                    {"field": "submitted_at", "type": "datetime"},
                    {"field": "note", "type": "long_text"},
                ),
                describe=lambda args: (
                    f"冻结投递 #{_payload(args).get('application_id')} 的实际简历、JD 和材料。"
                ),
                validate=_validate_snapshot,
                execute=lambda args: _execute_snapshot(outcomes, args),
            ),
            LegacyDeterministicAdapter(
                name="record_application_outcome",
                editable_fields=(
                    {"field": "stage", "type": "enum", "options": cast(JSONValue, sorted(STAGES))},
                    {
                        "field": "result",
                        "type": "enum",
                        "options": cast(JSONValue, sorted(RESULTS)),
                    },
                    {"field": "feedback_text", "type": "long_text"},
                    {"field": "reflection_text", "type": "long_text"},
                    {"field": "next_action_text", "type": "long_text"},
                    {"field": "occurred_at", "type": "datetime"},
                ),
                describe=lambda args: (
                    f"记录投递 #{_payload(args).get('application_id')} 的 {_payload(args).get('stage')} / {_payload(args).get('result')} 结果。"
                ),
                validate=_validate_outcome,
                execute=lambda args: _execute_outcome(outcomes, args),
            ),
        )
    )
