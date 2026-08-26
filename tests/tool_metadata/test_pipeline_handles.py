from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any, cast

import pytest

from offerpilot.ai.tool_authority import AuthorityPhaseError
from offerpilot.ai.tool_runtime import catalog as catalog_module
from offerpilot.ai.tool_runtime import pipeline as pipeline_module
from offerpilot.ai.tool_runtime.catalog import (
    SegmentToolCatalogLease,
    ToolCatalog,
)
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    ConfirmationRequired,
    ReadyToExecute,
    ToolFailure,
    ToolSuccess,
)
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_runtime.pipeline import execute_prepared, prepare_call
from offerpilot.ai.types import ToolCall
from tests.tool_metadata.factories import (
    compose_synthetic_bundle,
    write_metadata,
)
from tests.tool_pipeline.test_pipeline import (
    Recorder,
    Runtime,
    _arguments_digest,
    _runtime,
    _spec,
)


def _bundle_and_lease(
    spec: Any,
    runtime: Runtime | None = None,
) -> tuple[ToolMetadataBundleV1, SegmentToolCatalogLease]:
    source = compose_synthetic_bundle()
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    bundle = ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=source["manifest"],
        legacy_boundary=source["legacy_boundary"],
        compensation=source["compensation"],
    )
    lease = bundle.open_segment_lease()
    if runtime is not None:
        runtime.factory.bind_segment_tool_catalog(
            runtime.authority,
            authority_metadata_view=bundle.authority_view(),
            catalog_lease=lease,
        )
    return bundle, lease


def _clone_handle(handle: object) -> object:
    clone = object.__new__(type(handle))
    for name in getattr(type(handle), "__slots__", ()):
        if name != "__weakref__":
            object.__setattr__(clone, name, object.__getattribute__(handle, name))
    return clone


def _prepared_handle(prepared: object) -> object:
    return cast(Any, prepared).spec_handle


def _prepare_read(
    tmp_path: Any,
    *,
    recorder: Recorder,
    executor: Any | None = None,
    resolver: Any | None = None,
    preflight: Any | None = None,
) -> tuple[Runtime, ToolMetadataBundleV1, SegmentToolCatalogLease, Any, Any]:
    runtime = _runtime(tmp_path, recorder)
    spec = _spec(
        name="synthetic_tool",
        executor=executor,
        resolver=resolver,
        binding_contract=(
            BindingContract("enforce_if_bound", "application") if resolver is not None else None
        ),
        preflight=preflight,
    )
    bundle, lease = _bundle_and_lease(spec, runtime)
    call = ToolCall("route-call", spec.name, '{"id":1,"extra":"kept"}')
    prepared = prepare_call(
        lease,
        runtime.context,
        call,
        call_identity=runtime.prepare_identity(call),
    )
    assert isinstance(prepared, ReadyToExecute)
    return runtime, bundle, lease, call, prepared.prepared


def test_pipeline_parameter_mapping_is_exact_and_only_catalog_lookup_becomes_a_lease() -> None:
    prepare_parameters = tuple(inspect.signature(prepare_call).parameters)
    execute_parameters = tuple(inspect.signature(execute_prepared).parameters)

    assert prepare_parameters == (
        "catalog_lease",
        "context",
        "call",
        "call_identity",
        "pending_identity",
        "pending_action_revision",
        "stage_sink",
        "record_proposal",
    )
    assert execute_parameters == (
        "prepared",
        "context",
        "call_identity",
        "confirmation_claimer",
        "execution_claim",
        "locked_effective_args_digest",
        "stage_sink",
    )


def test_one_segment_opens_one_lease_and_resolves_and_parses_each_provider_call_once(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder)
    spec = _spec(name="synthetic_tool")
    source = compose_synthetic_bundle()
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    bundle = ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=source["manifest"],
        legacy_boundary=source["legacy_boundary"],
        compensation=source["compensation"],
    )
    lease_opens = 0
    resolves = 0
    parses = 0
    issued_handles: list[object] = []
    original_open = ToolMetadataBundleV1.open_segment_lease
    original_resolve = SegmentToolCatalogLease.resolve
    original_parse = pipeline_module.parse_arguments

    def counted_open(self: ToolMetadataBundleV1) -> SegmentToolCatalogLease:
        nonlocal lease_opens
        lease_opens += 1
        return original_open(self)

    def counted_resolve(self: SegmentToolCatalogLease, name: str) -> object | None:
        nonlocal resolves
        resolves += 1
        handle = original_resolve(self, name)
        if handle is not None:
            issued_handles.append(handle)
        return handle

    def counted_parse(raw: str) -> dict[str, Any]:
        nonlocal parses
        parses += 1
        return cast(dict[str, Any], original_parse(raw))

    monkeypatch.setattr(ToolMetadataBundleV1, "open_segment_lease", counted_open)
    monkeypatch.setattr(SegmentToolCatalogLease, "resolve", counted_resolve)
    monkeypatch.setattr(pipeline_module, "parse_arguments", counted_parse)

    try:
        lease = bundle.open_segment_lease()
        runtime.factory.bind_segment_tool_catalog(
            runtime.authority,
            authority_metadata_view=bundle.authority_view(),
            catalog_lease=lease,
        )
        call = ToolCall("once-call", spec.name, '{"id":1,"extra":"kept"}')
        result = prepare_call(
            lease,
            runtime.context,
            call,
            call_identity=runtime.prepare_identity(call),
        )

        assert isinstance(result, ReadyToExecute)
        assert lease_opens == 1
        assert resolves == 1
        assert parses == 1
        assert issued_handles == [_prepared_handle(result.prepared)]
        assert result.prepared.arguments == {"id": 1, "extra": "kept"}
    finally:
        runtime.close()


def test_prepare_rejects_raw_catalog_instead_of_opening_a_compatibility_route(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder)
    spec = _spec(name="synthetic_tool")
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    call = ToolCall("raw-catalog-call", spec.name, '{"id":1}')
    parses = 0
    original_parse = pipeline_module.parse_arguments

    def counted_parse(raw: str) -> Any:
        nonlocal parses
        parses += 1
        return original_parse(raw)

    monkeypatch.setattr(pipeline_module, "parse_arguments", counted_parse)
    try:
        with pytest.raises((AuthorityPhaseError, RuntimeError, TypeError, ValueError)):
            prepare_call(
                catalog,
                runtime.context,
                call,
                call_identity=runtime.prepare_identity(call),
            )
        assert parses == 0
    finally:
        runtime.close()


def test_prepare_rejects_a_lease_minted_outside_bundle_open_segment_lease(
    tmp_path: Any,
) -> None:
    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder)
    spec = _spec(name="synthetic_tool")
    source = compose_synthetic_bundle()
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    bundle = ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=source["manifest"],
        legacy_boundary=source["legacy_boundary"],
        compensation=source["compensation"],
    )
    private_issuer = getattr(catalog_module, "_open_segment_tool_catalog_lease")
    registered_lease = bundle.open_segment_lease()
    registered_handle = registered_lease.resolve(spec.name)
    assert registered_handle is not None
    unregistered_lease = private_issuer(
        catalog=catalog,
        bundle_instance_token=bundle.bundle_instance_token,
        generation=registered_lease.generation,
    )
    call = ToolCall("private-lease-call", spec.name, '{"id":1}')

    try:
        with pytest.raises(
            (RuntimeError, TypeError, ValueError), match="registered|lease|provenance"
        ):
            unregistered_lease.resolve(spec.name)
        with pytest.raises(
            (RuntimeError, TypeError, ValueError), match="registered|lease|provenance"
        ):
            unregistered_lease.require_spec(registered_handle)
        with pytest.raises((AuthorityPhaseError, RuntimeError, TypeError, ValueError)):
            prepare_call(
                unregistered_lease,
                runtime.context,
                call,
                call_identity=runtime.prepare_identity(call),
            )
        unregistered_lease.close()
        unregistered_lease.close()
    finally:
        runtime.close()


def test_prepare_preserves_pending_revision_stage_sink_and_proposal_recording(
    tmp_path: Any,
) -> None:
    recorder = Recorder()
    runtime = _runtime(
        tmp_path,
        recorder,
        capabilities=frozenset({"applications.write"}),
    )
    read_spec = _spec(name="synthetic_tool")
    metadata = replace(write_metadata("synthetic_tool"), editable_fields=())
    spec = replace(read_spec, metadata=metadata)
    _, lease = _bundle_and_lease(spec, runtime)
    pending = object()
    call = ToolCall("write-call", spec.name, '{"id":1}')
    stages: list[str] = []

    try:
        result = prepare_call(
            lease,
            runtime.context,
            call,
            call_identity=runtime.prepare_identity(call),
            pending_identity=pending,
            pending_action_revision=17,
            stage_sink=stages.append,
            record_proposal=True,
        )

        assert isinstance(result, ConfirmationRequired)
        assert result.prepared.pending_identity is pending
        assert result.prepared.pending_action_revision == 17
        assert [event.event_type for event in recorder.events] == ["tool.proposed"]
        assert stages == [
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
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "foreign_kind",
    ("copied", "same_bundle_cross_segment", "cross_bundle"),
)
def test_forged_route_from_resolve_fails_through_lease_before_any_tool_hook(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    foreign_kind: str,
) -> None:
    calls = {"resolver": 0, "preflight": 0, "executor": 0}

    def resolver(args: Any, context: Any) -> Any:
        calls["resolver"] += 1
        return context.binding_target_resolution(
            entity_kind="application", state="resolved", identity=args["id"]
        )

    def preflight(*_args: Any) -> None:
        calls["preflight"] += 1

    def executor(*_args: Any) -> dict[str, Any]:
        calls["executor"] += 1
        return {}

    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder)
    spec = _spec(
        name="synthetic_tool",
        resolver=resolver,
        binding_contract=BindingContract("enforce_if_bound", "application"),
        preflight=preflight,
        executor=executor,
    )
    bundle, lease = _bundle_and_lease(spec, runtime)
    genuine = lease.resolve(spec.name)
    assert genuine is not None
    if foreign_kind == "copied":
        injected = _clone_handle(genuine)
    elif foreign_kind == "same_bundle_cross_segment":
        sibling_lease = bundle.open_segment_lease()
        injected = sibling_lease.resolve(spec.name)
        assert injected is not None
    else:
        foreign_spec = _spec(name="synthetic_tool")
        _, foreign_lease = _bundle_and_lease(foreign_spec)
        injected = foreign_lease.resolve(foreign_spec.name)
        assert injected is not None

    original_resolve = SegmentToolCatalogLease.resolve
    original_require = SegmentToolCatalogLease.require_spec
    require_calls = 0
    digest_calls = 0
    stages: list[str] = []
    original_digest = pipeline_module._arguments_digest

    def inject_handle(self: SegmentToolCatalogLease, name: str) -> object | None:
        if self is lease and name == spec.name:
            return injected
        return original_resolve(self, name)

    def counted_require(self: SegmentToolCatalogLease, handle: object) -> Any:
        nonlocal require_calls
        if self is lease:
            require_calls += 1
        return original_require(self, handle)

    def counted_digest(arguments: dict[str, Any]) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return original_digest(arguments)

    monkeypatch.setattr(SegmentToolCatalogLease, "resolve", inject_handle)
    monkeypatch.setattr(SegmentToolCatalogLease, "require_spec", counted_require)
    monkeypatch.setattr(pipeline_module, "_arguments_digest", counted_digest)
    call = ToolCall("forged-call", spec.name, '{"id":1}')

    try:
        with pytest.raises((AuthorityPhaseError, RuntimeError, TypeError, ValueError)):
            prepare_call(
                lease,
                runtime.context,
                call,
                call_identity=runtime.prepare_identity(call),
                stage_sink=stages.append,
            )
        assert require_calls == 1
        assert calls == {"resolver": 0, "preflight": 0, "executor": 0}
        assert digest_calls == 0
        assert stages == []
        assert recorder.events == []
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "invalid_handle",
    ("copied", "closed", "same_bundle_cross_segment", "cross_bundle"),
)
def test_execute_revalidates_exact_live_handle_before_resolver_preflight_or_executor(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    invalid_handle: str,
) -> None:
    calls = {"resolver": 0, "preflight": 0, "executor": 0}

    def resolver(args: Any, context: Any) -> Any:
        calls["resolver"] += 1
        return context.binding_target_resolution(
            entity_kind="application", state="resolved", identity=args["id"]
        )

    def preflight(*_args: Any) -> None:
        calls["preflight"] += 1

    def executor(*_args: Any) -> dict[str, Any]:
        calls["executor"] += 1
        return {}

    recorder = Recorder()
    runtime, bundle, lease, _call, prepared = _prepare_read(
        tmp_path,
        recorder=recorder,
        resolver=resolver,
        preflight=preflight,
        executor=executor,
    )
    identity = runtime.read_identity(prepared)
    handle = _prepared_handle(prepared)
    calls.update(resolver=0, preflight=0, executor=0)
    recorder.events.clear()
    claim_calls = 0
    digest_calls = 0
    stages: list[str] = []
    original_digest = pipeline_module._typed_args_digest

    def claimer(_prepared: object) -> None:
        nonlocal claim_calls
        claim_calls += 1

    def counted_digest(value: object) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return original_digest(value)

    monkeypatch.setattr(pipeline_module, "_typed_args_digest", counted_digest)

    if invalid_handle == "closed":
        lease.close()
    elif invalid_handle == "copied":
        object.__setattr__(prepared, "spec_handle", _clone_handle(handle))
    elif invalid_handle == "same_bundle_cross_segment":
        sibling_lease = bundle.open_segment_lease()
        sibling_handle = sibling_lease.resolve(prepared.spec.name)
        assert sibling_handle is not None
        object.__setattr__(prepared, "spec_handle", sibling_handle)
    else:
        foreign_spec = _spec(name="synthetic_tool")
        _, foreign_lease = _bundle_and_lease(foreign_spec)
        foreign_handle = foreign_lease.resolve(foreign_spec.name)
        assert foreign_handle is not None
        object.__setattr__(prepared, "spec_handle", foreign_handle)

    try:
        with pytest.raises((AuthorityPhaseError, RuntimeError, TypeError, ValueError)):
            execute_prepared(
                prepared,
                runtime.context,
                call_identity=identity,
                confirmation_claimer=claimer,
                execution_claim=None,
                locked_effective_args_digest=None,
                stage_sink=stages.append,
            )
        assert calls == {"resolver": 0, "preflight": 0, "executor": 0}
        assert claim_calls == 0
        assert digest_calls == 0
        assert stages == []
        assert recorder.events == []
    finally:
        runtime.close()


@pytest.mark.parametrize("executor_raises", (False, True))
def test_execute_prepared_consumes_a_prepared_route_at_most_once(
    tmp_path: Any,
    executor_raises: bool,
) -> None:
    executor_calls = 0

    def executor(args: Any, _context: Any) -> dict[str, Any]:
        nonlocal executor_calls
        executor_calls += 1
        if executor_raises:
            raise RuntimeError("ordinary executor failure")
        return dict(args)

    recorder = Recorder()
    runtime, _bundle, _lease, _call, prepared = _prepare_read(
        tmp_path,
        recorder=recorder,
        executor=executor,
    )
    identity = runtime.read_identity(prepared)

    try:
        first = execute_prepared(
            prepared,
            runtime.context,
            call_identity=identity,
            confirmation_claimer=None,
            execution_claim=None,
            locked_effective_args_digest=None,
            stage_sink=None,
        )
        if executor_raises:
            assert isinstance(first.outcome, ToolFailure)
            assert first.outcome.code == "executor_exception"
        else:
            assert isinstance(first.outcome, ToolSuccess)

        with pytest.raises((AuthorityPhaseError, RuntimeError, TypeError, ValueError)):
            execute_prepared(
                prepared,
                runtime.context,
                call_identity=identity,
                confirmation_claimer=None,
                execution_claim=None,
                locked_effective_args_digest=None,
                stage_sink=None,
            )
        assert executor_calls == 1
    finally:
        runtime.close()


def test_call_identity_digest_is_still_exact_when_route_handle_replaces_name_lookup(
    tmp_path: Any,
) -> None:
    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder)
    spec = _spec(name="synthetic_tool")
    _, lease = _bundle_and_lease(spec, runtime)
    call = ToolCall("digest-call", spec.name, '{"id":1}')
    wrong_call = ToolCall("digest-call", spec.name, '{"id":2}')
    wrong_identity = runtime.factory.create_new_turn_prepare_identity(
        runtime.invocation,
        attempt_id=runtime.factory.issue_provider_attempt(
            runtime.invocation,
            candidate_ordinal=0,
        ),
        candidate_ordinal=0,
        tool_call_id=call.id,
        tool_name=call.name,
        arguments_digest=_arguments_digest(wrong_call.args),
    )

    try:
        with pytest.raises(AuthorityPhaseError):
            prepare_call(
                lease,
                runtime.context,
                call,
                call_identity=wrong_identity,
            )
    finally:
        runtime.close()
