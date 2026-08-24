from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from offerpilot.ai.tool_authority import AuthorityPhaseError
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.pipeline import execute_prepared, prepare_call
from offerpilot.ai.types import ToolCall
from tests.tool_pipeline.test_pipeline import Recorder, Runtime, _runtime, _spec


def _prepare(runtime: Runtime, spec: Any, call: ToolCall) -> Any:
    prepared = prepare_call(
        ToolCatalog([spec], expected_names=(spec.name,)),
        runtime.context,
        call,
        call_identity=runtime.prepare_identity(call),
    )
    return prepared.prepared


def _second_invocation(
    runtime: Runtime,
    *,
    tool_context: ToolExecutionContext | None = None,
) -> Any:
    factory = runtime.factory
    authority = runtime.authority
    context = runtime.context if tool_context is None else tool_context
    if tool_context is not None:
        factory.register_tool_execution_context(context, authority=authority)
    build = factory.create_provider_surface_build_identity(
        authority,
        runner_invocation=runtime.invocation.runner_invocation,
        tool_context=context,
        model_call_id=f"second-model-{id(context)}",
    )
    surface = object()
    binding = object()
    gateway = object()
    fingerprint = "sha256:" + "b" * 64
    factory.register_frozen_surface(
        surface,
        surface_fingerprint=fingerprint,
        candidate_count=1,
        authority=authority,
        build_identity=build,
    )
    factory.register_model_call_surface_binding(
        binding,
        surface=surface,
        surface_fingerprint=fingerprint,
        authority=authority,
        build_identity=build,
    )
    factory.register_gateway_session(
        gateway,
        authority=authority,
        build_identity=build,
        surface=surface,
        surface_fingerprint=fingerprint,
        model_call_surface_binding=binding,
    )
    return factory.create_provider_invocation_identity(
        build,
        surface=surface,
        surface_fingerprint=fingerprint,
        model_call_surface_binding=binding,
        gateway_session=gateway,
    )


def test_same_spec_can_be_prepared_twice_in_one_segment(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, Recorder())
    try:
        spec = _spec()
        first = ToolCall(id="repeat-1", name=spec.name, args='{"id":1}')
        second = ToolCall(id="repeat-2", name=spec.name, args='{"id":2}')

        assert _prepare(runtime, spec, first).tool_call_id == "repeat-1"
        assert _prepare(runtime, spec, second).tool_call_id == "repeat-2"
    finally:
        runtime.close()


def test_static_catalog_spec_can_be_used_by_concurrent_execution_scopes(
    tmp_path: Path,
) -> None:
    first_runtime = _runtime(tmp_path / "first", Recorder())
    second_runtime = _runtime(tmp_path / "second", Recorder())
    try:
        shared_spec = _spec()
        first = ToolCall(id="scope-1", name=shared_spec.name, args='{"id":1}')
        second = ToolCall(id="scope-2", name=shared_spec.name, args='{"id":2}')

        assert _prepare(first_runtime, shared_spec, first).tool_call_id == "scope-1"
        assert _prepare(second_runtime, shared_spec, second).tool_call_id == "scope-2"
    finally:
        first_runtime.close()
        second_runtime.close()


def test_read_identity_rejects_another_model_call_provenance(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, Recorder())
    try:
        spec = _spec()
        prepared = _prepare(
            runtime,
            spec,
            ToolCall(id="origin-call", name=spec.name, args='{"id":1}'),
        )
        another_invocation = _second_invocation(runtime)

        with pytest.raises(AuthorityPhaseError):
            runtime.factory.create_read_execution_identity(
                another_invocation,
                prepared=prepared,
                tool_call_id=prepared.tool_call_id,
                tool_name=prepared.spec.name,
                arguments_digest=prepared.arguments_digest,
            )
    finally:
        runtime.close()


def test_read_identity_rejects_another_context_provenance(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, Recorder())
    try:
        spec = _spec()
        prepared = _prepare(
            runtime,
            spec,
            ToolCall(id="origin-context-call", name=spec.name, args='{"id":1}'),
        )
        other_context = ToolExecutionContext(
            authority=runtime.authority,
            applications=runtime.context.applications,
            events=runtime.context.events,
            notes=runtime.context.notes,
            offers=runtime.context.offers,
            resumes=runtime.context.resumes,
            jd_analyses=runtime.context.jd_analyses,
            run_recorder=runtime.context.run_recorder,
        )
        another_invocation = _second_invocation(
            runtime,
            tool_context=other_context,
        )

        with pytest.raises(AuthorityPhaseError):
            runtime.factory.create_read_execution_identity(
                another_invocation,
                prepared=prepared,
                tool_call_id=prepared.tool_call_id,
                tool_name=prepared.spec.name,
                arguments_digest=prepared.arguments_digest,
            )
    finally:
        runtime.close()


def test_read_execute_rejects_mutated_typed_args_before_executor(tmp_path: Path) -> None:
    calls = 0

    def executor(args: dict[str, Any], _context: ToolExecutionContext) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return dict(args)

    runtime = _runtime(tmp_path, Recorder())
    try:
        spec = _spec(executor=executor)
        prepared = _prepare(
            runtime,
            spec,
            ToolCall(id="mutable-call", name=spec.name, args='{"id":1}'),
        )
        read_identity = runtime.read_identity(prepared)
        prepared.typed_args["id"] = 2

        with pytest.raises(AuthorityPhaseError):
            execute_prepared(
                prepared,
                runtime.context,
                call_identity=read_identity,
            )
        assert calls == 0
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "statement",
    (
        "import offerpilot.ai.tool_authority",
        "import offerpilot.ai.tool_authority.composition",
        "import offerpilot.ai.tool_runtime.context",
        "import offerpilot.ai.tool_runtime.pipeline",
        "from offerpilot.ai.tool_runtime import Rejected, execute_prepared, prepare_call",
    ),
)
def test_authority_and_runtime_modules_support_cold_imports(statement: str) -> None:
    project_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")

    completed = subprocess.run(
        [sys.executable, "-c", statement],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
