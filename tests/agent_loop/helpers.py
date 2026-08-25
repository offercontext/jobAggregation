from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal

from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.metadata import ToolPresentationBindingV1, WriteOperationMetadataV1
from offerpilot.ai.tool_runtime.policy_types import ToolCapability
from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_authority.policy import validate_startup_policy
from offerpilot.ai.tool_runtime.contracts import (
    ToolFailure,
)
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.types import Assistant
from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.db import init_database
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository


_DATA_DIR = Path(tempfile.mkdtemp(prefix="offerpilot-agent-loop-tests-"))
_SESSIONS = init_database(_DATA_DIR / "agent-loop.db")


def _default_raw_executor(_args: str) -> str:
    return "{}"


def _decode_arguments(values: dict[str, Any]) -> dict[str, Any]:
    return dict(values)


def _describe_update_application_status(_args: object) -> str:
    return "update_application_status"


def _describe_delete_note(_args: object) -> str:
    return "delete_note"


def _describe_test_read(_args: object) -> str:
    return "test read"


_CONFIRMATION_DESCRIPTIONS: dict[str, Callable[[object], str]] = {
    "delete_note": _describe_delete_note,
    "update_application_status": _describe_update_application_status,
}


def _test_presentation(
    name: str,
    original: ToolPresentationBindingV1,
) -> ToolPresentationBindingV1:
    return ToolPresentationBindingV1(
        implementation_id=f"agent_loop_test_{name}_presentation_v1",
        confirmation_description=_CONFIRMATION_DESCRIPTIONS.get(name, _describe_test_read),
        pending_details_projector=original.pending_details_projector,
        success_summary_projector=original.success_summary_projector,
    )


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    kind: Literal["read", "write"] = "read"
    executor: Callable[[str], str] = _default_raw_executor
    validator: Callable[[str], str] | None = None


def runtime(*definitions: ToolDefinition) -> tuple[ToolCatalog, ToolExecutionContext]:
    specs = list(MODEL_TOOL_CATALOG.specs)
    positions = {spec.name: index for index, spec in enumerate(specs)}
    for definition in definitions:
        position = positions.get(definition.name)
        if position is None:
            raise ValueError(f"test tool must use a model catalog name: {definition.name}")
        original = specs[position]
        actual_kind = (
            "write" if type(original.metadata.operation) is WriteOperationMetadataV1 else "read"
        )
        if definition.kind != actual_kind:
            raise ValueError(f"test tool kind differs from model catalog: {definition.name}")
        raw_executor = definition.executor
        raw_validator = definition.validator

        def executor(
            args: dict[str, Any],
            context: ToolExecutionContext,
            raw_executor: Callable[[str], str] = raw_executor,
        ) -> str:
            del context
            return raw_executor(json.dumps(args, ensure_ascii=False, separators=(",", ":")))

        def preflight(
            args: dict[str, Any],
            context: ToolExecutionContext,
            raw_validator: Callable[[str], str] | None = raw_validator,
        ) -> ToolFailure | None:
            del context
            if raw_validator is None:
                return None
            detail = raw_validator(json.dumps(args, ensure_ascii=False, separators=(",", ":")))
            return ToolFailure("validation_error", "test_validation", detail) if detail else None

        specs[position] = replace(
            original,
            decoder=_decode_arguments,
            executor=executor,
            preflight=preflight if raw_validator is not None else None,
            mutable_validator=preflight if raw_validator is not None else None,
            success_renderer=str,
            presentation=_test_presentation(definition.name, original.presentation),
        )
    catalog = ToolCatalog(specs, expected_names=tuple(spec.name for spec in specs))
    policy = validate_startup_policy(catalog.authority_manifest)
    authority_factory = AuthorityFactory()
    authority = authority_factory.create_segment_authority(
        conversation_id=1,
        conversation_scope_revision=0,
        segment_id="agent-loop-test-segment",
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        capabilities=frozenset(ToolCapability),
        capability_profile_id=policy.capability_profile.profile_id,
        capability_policy_version=policy.capability_policy_version,
        binding_policy_version=policy.binding_policy_version,
        capability_profile_fingerprint=policy.capability_profile_fingerprint,
        binding_policy_fingerprint=policy.binding_policy_fingerprint,
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=ApplicationsRepository(_SESSIONS),
        events=ApplicationEventsRepository(_SESSIONS),
        jd_analyses=JDAnalysesRepository(_SESSIONS),
        notes=NotesRepository(_SESSIONS),
        offers=OffersRepository(_SESSIONS),
        resumes=ResumesRepository(_SESSIONS),
        run_recorder=NullRunRecorder(),
    )
    return catalog, context


class ScriptedModel:
    def __init__(self, *turns: Assistant) -> None:
        self.turns = list(turns)
        self.calls = 0
        self.inputs: list[list[object]] = []

    def complete(self, messages: list[object], tools: list[object]) -> Assistant:
        del tools
        self.inputs.append(list(messages))
        value = self.turns[self.calls]
        self.calls += 1
        return value


class RecordingEventSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)
