from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    ProviderToolContract,
    ToolExceptionMapping,
    ToolFailure,
    ToolSpec,
    WriteContract,
)
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


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    kind: Literal["read", "write"] = "read"
    executor: Callable[[str], str] = lambda _args: "{}"
    validator: Callable[[str], str] | None = None


def runtime(*definitions: ToolDefinition) -> tuple[ToolCatalog, ToolExecutionContext]:
    specs: list[ToolSpec[Any, Any]] = []
    names: list[str] = []
    for definition in definitions:
        names.append(definition.name)
        schema: Mapping[str, Any] = {"type": "object", "properties": {}}
        contract = ProviderToolContract(
            payload={
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.name,
                    "parameters": schema,
                },
            },
            name=definition.name,
            description=definition.name,
            parameters=schema,
        )
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

        is_write = definition.kind == "write"
        specs.append(
            ToolSpec(
                contract=contract,
                kind=definition.kind,
                decoder=lambda values: dict(values),
                executor=executor,
                confirmation_policy="required" if is_write else "none",
                preflight=preflight if raw_validator is not None else None,
                mutable_validator=preflight if raw_validator is not None else None,
                declared_failure_categories=frozenset({"validation_error", "internal_error"}),
                exception_map=(
                    ToolExceptionMapping(Exception, "internal_error", "test_handler_error", str),
                ),
                success_renderer=str,
                confirmation_description=lambda _args, name=definition.name: name,
                write_contract=WriteContract() if is_write else None,
            )
        )
    catalog = ToolCatalog(specs, expected_names=names)
    context = ToolExecutionContext(
        applications=ApplicationsRepository(_SESSIONS),
        capabilities=frozenset(ToolCapability),
        current_bindings={},
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
