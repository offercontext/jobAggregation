from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, cast

import pytest

from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    ProviderToolContract,
    ReadyToExecute,
    ToolFailure,
    ToolSpec,
    ToolSuccess,
)
from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    ResolverImplementationBinding,
    ToolBindingMetadataV1,
)
from offerpilot.ai.tool_runtime.policy_types import ToolCapability
from offerpilot.ai.tool_runtime.pipeline import Rejected, execute_prepared, prepare_call
from offerpilot.ai.types import ToolCall
from offerpilot.db import init_database
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository
from tests.tool_metadata.factories import presentation_binding, read_metadata


class Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.recording_status = "healthy"

    def append_event(self, event: Any) -> None:
        self.events.append(event)


_RESOLVER_PROBE: Any | None = None


def _resolve_with_probe(args: Any, context: ToolExecutionContext) -> Any:
    if _RESOLVER_PROBE is None:
        raise AssertionError("resolver probe is not configured")
    return _RESOLVER_PROBE(args, context)


def _decode_mapping(values: Any) -> dict[str, Any]:
    return dict(values)


def _render_mapping(result: Any) -> str:
    return str(result)


@dataclass
class Runtime:
    factory: AuthorityFactory
    authority: Any
    context: ToolExecutionContext
    invocation: Any
    session_factory: Any

    def prepare_identity(self, call: ToolCall) -> Any:
        attempt = self.factory.issue_provider_attempt(self.invocation, candidate_ordinal=0)
        return self.factory.create_new_turn_prepare_identity(
            self.invocation,
            attempt_id=attempt,
            candidate_ordinal=0,
            tool_call_id=call.id,
            tool_name=call.name,
            arguments_digest=_arguments_digest(call.args),
        )

    def read_identity(self, prepared: Any) -> Any:
        return self.factory.create_read_execution_identity(
            self.invocation,
            prepared=prepared,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=prepared.arguments_digest,
        )

    def close(self) -> None:
        self.factory.close()
        self.session_factory.kw["bind"].dispose()


def _arguments_digest(raw: str) -> str:
    try:
        value = json.loads(raw)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except Exception:
        encoded = raw.encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _runtime(
    tmp_path: Any,
    recorder: Recorder,
    *,
    capabilities: frozenset[str] = frozenset({"applications.read"}),
    context_type: str = "workspace",
) -> Runtime:
    session_factory = init_database(tmp_path / f"pipeline-{id(recorder)}.db")
    factory = AuthorityFactory()
    authority = factory.create_segment_authority(
        conversation_id=1,
        conversation_scope_revision=0,
        segment_id="segment-pipeline",
        trusted_scope=TrustedContextScope(
            cast(Any, context_type),
            1 if context_type == "application" else None,
            "general",
        ),
        capabilities=capabilities,
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=ApplicationsRepository(session_factory),
        events=ApplicationEventsRepository(session_factory),
        notes=NotesRepository(session_factory),
        offers=OffersRepository(session_factory),
        resumes=ResumesRepository(session_factory),
        jd_analyses=JDAnalysesRepository(session_factory),
        run_recorder=cast(Any, recorder),
    )
    runner = object()
    surface = object()
    binding = object()
    gateway = object()
    factory.register_runner_invocation(runner, authority=authority)
    factory.register_tool_execution_context(context, authority=authority)
    build = factory.create_provider_surface_build_identity(
        authority,
        runner_invocation=runner,
        tool_context=context,
        model_call_id="model-pipeline",
    )
    fingerprint = "sha256:" + "a" * 64
    factory.register_frozen_surface(
        surface,
        surface_fingerprint=fingerprint,
        candidate_count=32,
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
    invocation = factory.create_provider_invocation_identity(
        build,
        surface=surface,
        surface_fingerprint=fingerprint,
        model_call_surface_binding=binding,
        gateway_session=gateway,
    )
    return Runtime(factory, authority, context, invocation, session_factory)


def _spec(
    *,
    name: str = "read_application",
    executor: Any | None = None,
    resolver: Any | None = None,
    binding_contract: BindingContract | None = None,
    preflight: Any | None = None,
    required_capabilities: frozenset[str] = frozenset({"applications.read"}),
) -> ToolSpec[dict[str, Any], dict[str, Any]]:
    global _RESOLVER_PROBE

    parameters = {
        "additionalProperties": True,
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
        "type": "object",
    }
    descriptor = None
    resolver_bindings = ()
    if resolver is not None:
        _RESOLVER_PROBE = resolver
        descriptor = BindingResolverDescriptorV1(
            resolver_id="application_identity_arg",
            entity_kind="application",
            arg_path="id",
            presence="required",
            identity_type="positive_int64",
        )
        resolver_bindings = (
            ResolverImplementationBinding(
                descriptor=descriptor,
                implementation_id="pipeline_probe_resolver_v1",
                resolve=_resolve_with_probe,
            ),
        )
    final_binding_contract = binding_contract or BindingContract()
    surface = replace(
        read_metadata(),
        required_capabilities=tuple(
            ToolCapability(capability) for capability in required_capabilities
        ),
        binding=ToolBindingMetadataV1(
            contract=final_binding_contract,
            resolver_descriptors=() if descriptor is None else (descriptor,),
        ),
    )
    return ToolSpec(
        contract=ProviderToolContract(
            payload={
                "type": "function",
                "function": {
                    "description": name,
                    "name": name,
                    "parameters": parameters,
                },
            },
            name=name,
            description=name,
            parameters=parameters,
        ),
        metadata=surface,
        resolver_bindings=resolver_bindings,
        undo_builder_binding=None,
        decoder=_decode_mapping,
        executor=executor or (lambda args, context: args),
        presentation=presentation_binding(),
        preflight=preflight,
        success_renderer=_render_mapping,
    )


def test_prepare_and_read_execute_have_exact_stage_order(tmp_path: Any) -> None:
    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder)
    try:
        trace: list[str] = []
        spec = _spec()
        call = ToolCall(id="read-1", name=spec.name, args='{"id":1,"extra":"kept"}')
        prepared = prepare_call(
            ToolCatalog([spec], expected_names=(spec.name,)),
            runtime.context,
            call,
            call_identity=runtime.prepare_identity(call),
            stage_sink=trace.append,
        )

        assert isinstance(prepared, ReadyToExecute)
        assert trace == [
            "authority.prelookup",
            "catalog.lookup",
            "authority.postlookup",
            "parse",
            "schema",
            "decode",
            "capability",
            "scope_policy",
            "binding.resolve",
            "binding.policy",
            "preflight",
            "prepared",
        ]
        assert prepared.prepared.arguments == {"id": 1, "extra": "kept"}

        trace.clear()
        record = execute_prepared(
            prepared.prepared,
            runtime.context,
            call_identity=runtime.read_identity(prepared.prepared),
            stage_sink=trace.append,
        )
        assert isinstance(record.outcome, ToolSuccess)
        assert trace == [
            "authority.prelookup",
            "authority.postlookup",
            "capability",
            "binding.resolve",
            "binding.policy",
            "binding.rollback",
            "tool.started",
            "executor",
            "tool.completed",
        ]
    finally:
        runtime.close()


def test_unknown_tool_has_catalog_lookup_only_and_no_proposal(tmp_path: Any) -> None:
    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder)
    try:
        trace: list[str] = []
        spec = _spec()
        call = ToolCall(id="unknown-1", name="unknown", args='{"id":1}')
        result = prepare_call(
            ToolCatalog([spec], expected_names=(spec.name,)),
            runtime.context,
            call,
            call_identity=runtime.prepare_identity(call),
            stage_sink=trace.append,
        )
        assert isinstance(result, Rejected)
        assert result.failure == ToolFailure(
            "validation_error", "unknown_tool", '未知工具 "unknown"'
        )
        assert trace == ["authority.prelookup", "catalog.lookup"]
        assert recorder.events == []
    finally:
        runtime.close()


def test_missing_capability_has_zero_resolver_repository_preflight_executor_calls(
    tmp_path: Any,
) -> None:
    calls = {"resolver": 0, "repository": 0, "preflight": 0, "executor": 0}

    def resolver(args: Any, context: ToolExecutionContext) -> Any:
        calls["resolver"] += 1
        context.applications.get(args["id"])
        calls["repository"] += 1
        return context.binding_target_resolution(
            entity_kind="application", state="resolved", identity=args["id"]
        )

    def preflight(*_args: Any) -> None:
        calls["preflight"] += 1

    def executor(*_args: Any) -> dict[str, Any]:
        calls["executor"] += 1
        return {}

    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder, capabilities=frozenset())
    try:
        spec = _spec(
            resolver=resolver,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            preflight=preflight,
            executor=executor,
        )
        call = ToolCall(id="read-1", name=spec.name, args='{"id":1}')
        result = prepare_call(
            ToolCatalog([spec], expected_names=(spec.name,)),
            runtime.context,
            call,
            call_identity=runtime.prepare_identity(call),
        )
        assert isinstance(result, Rejected)
        assert result.failure.code == "missing_capability"
        assert calls == {"resolver": 0, "repository": 0, "preflight": 0, "executor": 0}
    finally:
        runtime.close()


def test_non_application_only_denies_before_resolver(tmp_path: Any) -> None:
    calls = 0

    def forbidden(*_args: Any) -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("resolver/preflight/executor must not run")

    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder, context_type="application")
    try:
        spec = _spec(
            name="create_application",
            executor=forbidden,
            binding_contract=BindingContract("non_application_only"),
            preflight=forbidden,
        )
        call = ToolCall(id="call-1", name=spec.name, args='{"id":1}')
        trace: list[str] = []
        result = prepare_call(
            ToolCatalog([spec], expected_names=(spec.name,)),
            runtime.context,
            call,
            call_identity=runtime.prepare_identity(call),
            stage_sink=trace.append,
        )
        assert isinstance(result, Rejected)
        assert result.failure == ToolFailure(
            "permission_denied", "scope_access_denied", "permission denied"
        )
        assert trace[-1] == "scope_policy"
        assert calls == 0
    finally:
        runtime.close()


def test_resolver_exception_maps_safely_and_base_exception_propagates(tmp_path: Any) -> None:
    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder)
    try:
        for error in (RuntimeError("private sql detail"),):
            def fail(*_args: Any, error: Exception = error) -> Any:
                raise error

            spec = _spec(
                resolver=fail,
                binding_contract=BindingContract("enforce_if_bound", "application"),
            )
            call = ToolCall(id="read-exception", name=spec.name, args='{"id":1}')
            result = prepare_call(
                ToolCatalog([spec], expected_names=(spec.name,)),
                runtime.context,
                call,
                call_identity=runtime.prepare_identity(call),
            )
            assert isinstance(result, Rejected)
            assert result.failure == ToolFailure("internal_error", "binding_resolution_failed")
            assert "private" not in repr(result.failure)

        stop = BaseException("stop")

        def stop_resolver(*_args: Any) -> Any:
            raise stop

        spec = _spec(
            resolver=stop_resolver,
            binding_contract=BindingContract("enforce_if_bound", "application"),
        )
        call = ToolCall(id="read-base", name=spec.name, args='{"id":1}')
        with pytest.raises(BaseException) as raised:
            prepare_call(
                ToolCatalog([spec], expected_names=(spec.name,)),
                runtime.context,
                call,
                call_identity=runtime.prepare_identity(call),
            )
        assert raised.value is stop
    finally:
        runtime.close()


def test_read_execution_scope_denial_is_started_failure_with_compatibility_shape(
    tmp_path: Any,
) -> None:
    from offerpilot.ai.tool_runtime.rendering import render_compatibility
    from offerpilot.repositories.session_binding import ScopeAccessDenied

    def deny(*_args: Any) -> Any:
        raise ScopeAccessDenied("private target state")

    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder)
    try:
        spec = _spec(executor=deny)
        call = ToolCall(id="read-denied", name=spec.name, args='{"id":1}')
        prepared = prepare_call(
            ToolCatalog([spec], expected_names=(spec.name,)),
            runtime.context,
            call,
            call_identity=runtime.prepare_identity(call),
        )
        assert isinstance(prepared, ReadyToExecute)
        record = execute_prepared(
            prepared.prepared,
            runtime.context,
            call_identity=runtime.read_identity(prepared.prepared),
        )
        assert record.execution_started is True
        assert record.outcome == ToolFailure(
            "permission_denied", "scope_access_denied", "permission denied"
        )
        assert render_compatibility(spec, record.outcome) == "错误：permission denied"
        assert [event.event_type for event in recorder.events] == [
            "tool.proposed",
            "tool.started",
            "tool.failed",
        ]
        assert recorder.events[-1].facts["failure_category"] == "tool_error"
    finally:
        runtime.close()
