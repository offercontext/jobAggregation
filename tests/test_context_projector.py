from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import sqlite3
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from fastapi.testclient import TestClient

import offerpilot.context_projector.manifest as manifest_module
from offerpilot.agent_runtime.events import (
    JournalEventValidationError,
    validate_context_manifest_json,
)
from offerpilot.agent_runtime.budget import JournalBudgetExhausted
from offerpilot.agent_runtime.journal import RunRecorderFactory
from offerpilot.agent_runtime.keyring import load_or_create_journal_key
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG, MODEL_TOOL_NAMES
from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_authority.policy import (
    AGENT_TYPED_V1_PROFILE,
)
from offerpilot.ai.tool_authority.composition import AuthorityFactory
from offerpilot.ai.tool_authority.contracts import TrustedContextScope
from offerpilot.ai.types import Assistant, Message, ToolCall
from offerpilot.context_projector.binding import (
    BoundProviderResponse,
    ModelCallSurfaceBinding,
)
from offerpilot.context_projector.chunking import chunk_structured_source
from offerpilot.context_projector.budget import (
    OPTIONAL_HISTORY_MESSAGE_BYTE_CAP,
    ProviderBudget,
    optional_shares,
)
from offerpilot.context_projector.contracts import (
    CONTRIBUTOR_ORDER,
    ContributorResult,
    FrozenMessage,
    FrozenModelSurface,
    FrozenSource,
    ProjectionError,
    RuntimeSourceAudit,
    RuntimeSurfaceAudit,
    SourceChunk,
    canonical_json,
)
from offerpilot.context_projector.gateway import (
    AgentProviderGatewaySession,
    FrozenProviderExecutionChain,
    SingleCandidateAgentTransport,
    normalize_provider_endpoint,
)
from offerpilot.context_projector.history import group_history, select_history
from offerpilot.context_projector.loader import ContextSourceLoader, fetch_rows
from offerpilot.context_projector.manifest import (
    MANIFEST_SIGNAL_VALUES,
    ManifestV2ValidationError,
    _identity,
    prepare_surface_manifest_v2,
    validate_surface_manifest_v2,
)
from offerpilot.context_projector.projector import ModelSurfaceProjector, ProjectionRequest
from offerpilot.context_projector.selector import ToolSelectionSignals, select_tools
from offerpilot.context_projector.signals import RuntimeSignalSink
from offerpilot.pilot_runtime.errors import (
    RuntimeAgentTimedOut,
    RuntimeCancelled,
    RuntimeTransportAborted,
)
from offerpilot.pilot_runtime.compensation import prepare_compensation_handler_components
from offerpilot.config import AIProviderProfile, Config, save_config
from offerpilot.db import init_database, journal_session_factory_for_data_dir
from offerpilot.models import AgentContextSnapshot, AgentEvent, AgentRun, Conversation
from offerpilot.repositories.agent_runs import AgentRunRepository
from offerpilot.api import create_app


def _selector_bundle() -> ToolMetadataBundleV1:
    manifest = compile_tool_metadata_manifest(MODEL_TOOL_CATALOG.specs)
    projection = manifest.to_dict()
    compensation = prepare_compensation_handler_components()
    return ToolMetadataBundleV1(
        typed_catalog=MODEL_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=cast(dict[str, object], projection["legacy_boundary"]),
        compensation=compensation.metadata_projection(),
    )


def _selection_for(signals: ToolSelectionSignals) -> object:
    bundle = _selector_bundle()
    return select_tools(bundle.discovery_view(), bundle.authority_view(), signals)


_GATEWAY_AUTHORITY_FACTORIES: list[AuthorityFactory] = []


@pytest.fixture(scope="module", autouse=True)
def _close_gateway_authority_factories() -> object:
    yield
    for factory in reversed(_GATEWAY_AUTHORITY_FACTORIES):
        factory.close()
    _GATEWAY_AUTHORITY_FACTORIES.clear()


def _authorize_gateway_surface(
    surface: FrozenModelSurface,
    gateway: AgentProviderGatewaySession,
) -> tuple[FrozenModelSurface, object, ModelCallSurfaceBinding]:
    factory = AuthorityFactory()
    _GATEWAY_AUTHORITY_FACTORIES.append(factory)
    authority = factory.create_segment_authority(
        conversation_id=1,
        conversation_scope_revision=0,
        segment_id=f"segment-{len(_GATEWAY_AUTHORITY_FACTORIES)}",
        trusted_scope=TrustedContextScope(
            context_type="workspace", context_ref=None, mode="general"
        ),
        capabilities=frozenset(AGENT_TYPED_V1_PROFILE.capabilities),
    )
    runner = object()
    context = SimpleNamespace(authority=authority, authority_factory=factory)
    factory.register_runner_invocation(runner, authority=authority)
    factory.register_tool_execution_context(context, authority=authority)
    build = factory.create_provider_surface_build_identity(
        authority,
        runner_invocation=runner,
        tool_context=context,
        model_call_id=surface.model_call_id,
    )
    authorized = replace(surface, provider_surface_build_identity=build)
    binding = ModelCallSurfaceBinding.from_surface(authorized)
    invocation = gateway.bind_provider_surface(
        authority=authority,
        build_identity=build,
        surface=authorized,
        model_call_surface_binding=binding,
    )
    return authorized, invocation, binding


class FailingGuard:
    def __init__(self, fail_at: int) -> None:
        self.fail_at = fail_at
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls == self.fail_at:
            raise JournalBudgetExhausted


class RecordingDigest:
    def __init__(self, delegate, chunks: list[bytes]) -> None:
        self._delegate = delegate
        self._chunks = chunks

    def update(self, chunk: bytes) -> None:
        self._chunks.append(bytes(chunk))
        self._delegate.update(chunk)

    def hexdigest(self) -> str:
        return self._delegate.hexdigest()

    def digest(self) -> bytes:
        return self._delegate.digest()

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


def _non_advancing_journal_clock() -> float:
    return 0.0


def _projector_journal_factory(data_dir: Path) -> RunRecorderFactory:
    repository = AgentRunRepository(journal_session_factory_for_data_dir(data_dir))
    key = load_or_create_journal_key(data_dir)
    assert key is not None
    return RunRecorderFactory(
        repository,
        key=key,
        clock=_non_advancing_journal_clock,
        segment_budget_seconds=2.0,
        disposition_budget_seconds=0.5,
    )


def _wait_for_projector_journal_completion(
    data_dir: Path, *, timeout: float = 15.0
) -> tuple[list[AgentRun], list[AgentEvent], list[AgentContextSnapshot]]:
    factory = init_database(data_dir / "data.db")
    deadline = time.monotonic() + timeout
    run: AgentRun | None = None
    events: list[AgentEvent] = []
    snapshots: list[AgentContextSnapshot] = []
    while True:
        with factory() as session:
            run = session.scalar(select(AgentRun).order_by(AgentRun.started_at, AgentRun.id))
            if run is None:
                events = []
                snapshots = []
            else:
                events = list(
                    session.scalars(
                        select(AgentEvent)
                        .where(AgentEvent.run_id == run.id)
                        .order_by(AgentEvent.seq)
                    )
                )
                snapshots = list(
                    session.scalars(
                        select(AgentContextSnapshot)
                        .where(AgentContextSnapshot.run_id == run.id)
                        .order_by(AgentContextSnapshot.created_at)
                    )
                )
            if (
                run is not None
                and run.status == "completed"
                and events
                and any(event.event_type == "run.completed" for event in events)
                and events[-1].event_type == "segment.finished"
                and any(snapshot.snapshot_kind == "model_input" for snapshot in snapshots)
            ):
                return [run], events, snapshots
        if time.monotonic() >= deadline:
            pytest.fail(
                "projector Journal did not converge: "
                f"statuses={[run.status] if run is not None else []!r}, "
                f"events={[event.event_type for event in events]!r}, "
                f"snapshots={[snapshot.snapshot_kind for snapshot in snapshots]!r}"
            )
        time.sleep(0.01)


def _recording_sha256(chunks: list[bytes]):
    def factory(initial: bytes = b"") -> RecordingDigest:
        digest = RecordingDigest(hashlib.sha256(), chunks)
        if initial:
            digest.update(initial)
        return digest

    return factory


def frozen(role: str, content: str = "", *, message_id: int = 0) -> FrozenMessage:
    return FrozenMessage.freeze(Message(role=role, content=content), source_message_id=message_id)


def contributors(request: str = "比较 offer") -> tuple[ContributorResult, ...]:
    values = []
    for name in CONTRIBUTOR_ORDER:
        status = (
            "disabled"
            if name
            in {
                "confirmed_memory",
                "knowledge_context",
                "older_conversation_summary",
            }
            else "not_applicable"
        )
        messages = ()
        if name == "static_policy":
            status, messages = "ready", (frozen("system", "policy"),)
        elif name == "current_request":
            status, messages = "ready", (frozen("user", request),)
        values.append(ContributorResult(name, status, messages))
    return tuple(values)


def test_canonical_contract_rejects_runtime_objects_and_non_finite_numbers() -> None:
    with pytest.raises(ProjectionError, match="non_primitive_source_value"):
        canonical_json({"session": object()})
    with pytest.raises(ProjectionError, match="non_canonical_number"):
        canonical_json({"value": float("nan")})


def test_frozen_source_has_distinct_revision_and_full_content_fingerprint() -> None:
    source = FrozenSource.present(
        kind="application", revision_identity="revision:7", content={"x": 1}
    )
    changed = FrozenSource.present(
        kind="application", revision_identity="revision:7", content={"x": 2}
    )
    assert source.revision_identity == changed.revision_identity
    assert source.content_revision_fingerprint != changed.content_revision_fingerprint
    with pytest.raises(FrozenInstanceError):
        source.kind = "changed"  # type: ignore[misc]


def test_contributor_diagnostics_are_closed_and_bounded() -> None:
    with pytest.raises(ProjectionError, match="invalid_diagnostic"):
        ContributorResult("current_scope", "ready", diagnostics={"detail": "secret"})  # type: ignore[dict-item]


@pytest.mark.parametrize(
    "value",
    [
        "ftp://example.com/v1",
        "https://user:pass@example.com/v1",
        "https://example.com/v1?q=x",
        "https://example.com/v1#x",
        "https://example.com\\v1",
        "https://example.com/a/../b",
        "https://[fe80::1%25eth0]/v1",
    ],
)
def test_endpoint_normalization_rejects_ambiguous_destinations(value: str) -> None:
    with pytest.raises(ProjectionError):
        normalize_provider_endpoint(value)


def test_endpoint_normalization_is_strict_and_stable() -> None:
    assert normalize_provider_endpoint("HTTPS://EXAMPLE.COM:443/v1/") == "https://example.com/v1"
    assert normalize_provider_endpoint("http://example.com:8080/v1") == "http://example.com:8080/v1"


def test_tool_selector_uses_original_catalog_order_and_dependency_closure() -> None:
    bundle = _selector_bundle()
    selection = select_tools(
        bundle.discovery_view(),
        bundle.authority_view(),
        ToolSelectionSignals(page_kind="offers", current_request="比较薪资"),
    )
    assert selection.selected_names == tuple(
        name for name in MODEL_TOOL_NAMES if name in selection.selected_names
    )
    assert {"list_offers", "get_offer", "compare_offers"}.issubset(selection.dependency_closure)
    assert len(selection.provider_envelope_fingerprint) == 64


def test_tool_selector_falls_back_to_all_typed_tools_and_fails_on_bad_signal() -> None:
    bundle = _selector_bundle()
    selection = select_tools(
        bundle.discovery_view(),
        bundle.authority_view(),
        ToolSelectionSignals(page_kind="workspace"),
    )
    assert selection.selected_names == MODEL_TOOL_NAMES
    assert selection.full_catalog_fallback is True
    with pytest.raises(ProjectionError, match="unknown_page_kind"):
        select_tools(
            bundle.discovery_view(),
            bundle.authority_view(),
            ToolSelectionSignals(page_kind="evil"),
        )


def test_turn_group_is_atomic_and_orphan_tool_result_fails_closed() -> None:
    messages = (
        FrozenMessage.freeze(Message("user", "first"), source_message_id=1),
        FrozenMessage.freeze(
            Message("assistant", tool_calls=[ToolCall("c1", "list_offers", "{}")]),
            source_message_id=2,
        ),
        FrozenMessage.freeze(Message("tool", "[]", tool_call_id="c1"), source_message_id=3),
        FrozenMessage.freeze(Message("assistant", "done"), source_message_id=4),
        FrozenMessage.freeze(Message("user", "second"), source_message_id=5),
        FrozenMessage.freeze(Message("assistant", "reply"), source_message_id=6),
    )
    groups = group_history(messages)
    assert [len(group.messages) for group in groups] == [4, 2]
    with pytest.raises(ProjectionError, match="orphan_tool_message"):
        group_history((FrozenMessage.freeze(Message("tool", "x", tool_call_id="missing")),))


def test_history_skips_oversize_or_nonfitting_group_and_continues() -> None:
    groups = group_history(
        (
            frozen("user", "offer details", message_id=1),
            frozen("assistant", "x" * 500, message_id=2),
            frozen("user", "offer", message_id=3),
            frozen("assistant", "short", message_id=4),
        )
    )
    selected = select_history(groups, current_request="offer", budget_bytes=200)
    assert [group.last_message_id for group in selected] == [4]


@pytest.mark.parametrize("large_field", ["tool_args", "provider_blocks"])
def test_history_marks_complete_canonical_message_over_one_mib_as_oversized(
    large_field: str,
) -> None:
    oversized = "x" * (OPTIONAL_HISTORY_MESSAGE_BYTE_CAP + 1)
    if large_field == "tool_args":
        messages = (
            frozen("user", "request", message_id=1),
            FrozenMessage.freeze(
                Message(
                    "assistant",
                    tool_calls=[ToolCall("large", "list_offers", oversized)],
                ),
                source_message_id=2,
            ),
            FrozenMessage.freeze(Message("tool", "[]", tool_call_id="large"), source_message_id=3),
        )
    else:
        messages = (
            frozen("user", "request", message_id=1),
            FrozenMessage.freeze(
                Message("assistant", "ok", provider_blocks={"reasoning": oversized}),
                source_message_id=2,
            ),
        )

    assert group_history(messages)[0].oversized is True


def test_budget_rounding_remainder_enters_shared_pool() -> None:
    shares, pool = optional_shares(11)
    assert shares == {"scope": 2, "attachments": 3, "history": 4}
    assert pool == 2


def test_structured_chunker_records_paths_sizes_and_truncation() -> None:
    chunks = chunk_structured_source({"resume": {"summary": "求" * 100}}, byte_cap=64, max_chunks=2)
    assert len(chunks) == 2
    assert chunks[0].path == "$.resume.summary"
    assert chunks[0].original_bytes == 300
    assert chunks[0].original_codepoints == 100
    assert chunks[-1].truncated is True


def test_projection_is_repeatable_and_preserves_current_request() -> None:
    bundle = _selector_bundle()
    signals = ToolSelectionSignals(page_kind="offers", current_request="比较 offer")
    selection = select_tools(bundle.discovery_view(), bundle.authority_view(), signals)
    request = ProjectionRequest(
        model_call_id="call-1",
        contributors=contributors(),
        history=(frozen("user", "old", message_id=1), frozen("assistant", "answer", message_id=2)),
        tool_signals=signals,
        provider_budgets=(ProviderBudget(),),
        selection=selection,
    )
    first = ModelSurfaceProjector().project(request)
    second = ModelSurfaceProjector().project(request)
    assert first.runtime_surface_fingerprint == second.runtime_surface_fingerprint
    assert first.messages[-1].content == "比较 offer"
    assert first.tools is selection.provider_contracts
    assert first.audit.selected_tool_names == selection.selected_names


def test_projection_mandatory_overflow_fails_before_provider() -> None:
    signals = ToolSelectionSignals(current_request="offer")
    request = ProjectionRequest(
        model_call_id="call-1",
        contributors=contributors("x" * 40_000),
        history=(),
        tool_signals=signals,
        provider_budgets=(ProviderBudget(context_window=10_000),),
        selection=_selection_for(signals),
    )
    with pytest.raises(ProjectionError, match="mandatory_surface_over_budget"):
        ModelSurfaceProjector().project(request)


def test_bound_response_rejects_unexposed_tool_without_executor() -> None:
    signals = ToolSelectionSignals(page_kind="offers", current_request="offer")
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-1",
            contributors(),
            (),
            signals,
            (ProviderBudget(),),
            _selection_for(signals),
        )
    )
    chain = FrozenProviderExecutionChain.freeze(
        [AIProviderProfile(id="bound", api_key="x", base_url="https://bound.test/v1")]
    )
    gateway = AgentProviderGatewaySession(
        chain,
        SingleCandidateAgentTransport(
            lambda *_args: Assistant(),
            lambda *_args: Assistant(),
        ),
    )
    surface, invocation, binding = _authorize_gateway_surface(surface, gateway)
    attempt = invocation.tool_context.authority_factory.issue_provider_attempt(
        invocation, candidate_ordinal=0
    )
    response = BoundProviderResponse(
        "call-1",
        0,
        attempt,
        surface.runtime_surface_fingerprint,
        Assistant(tool_calls=[ToolCall("x", "delete_note", "{}")]),
        invocation,
        binding,
    )
    with pytest.raises(ProjectionError, match="unknown_tool"):
        binding.validate_response(response, attempt_validator=lambda value: value == attempt)


@pytest.mark.parametrize(
    "values",
    [
        (1, 0, "attempt", "f" * 64, Assistant()),
        ("call", True, "attempt", "f" * 64, Assistant()),
        ("call", 0, 1, "f" * 64, Assistant()),
        ("call", 0, "attempt", b"f" * 64, Assistant()),
        ("call", 0, "attempt", "f" * 64, object()),
    ],
)
def test_bound_provider_response_rejects_malformed_provenance_types(
    values: tuple[object, object, object, object, object],
) -> None:
    with pytest.raises(ProjectionError, match="invalid_bound_provider_response"):
        BoundProviderResponse(*values, object(), object())  # type: ignore[arg-type]


def test_gateway_reuses_surface_and_stops_stream_fallback_after_delta() -> None:
    profiles = [
        AIProviderProfile(id="a", api_key="a", base_url="https://a.test/v1"),
        AIProviderProfile(id="b", api_key="b", base_url="https://b.test/v1"),
    ]
    chain = FrozenProviderExecutionChain.freeze(profiles)
    signals = ToolSelectionSignals(page_kind="offers", current_request="offer")
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-1",
            contributors(),
            (),
            signals,
            tuple(candidate.budget() for candidate in chain.candidates),
            _selection_for(signals),
        )
    )
    calls: list[str] = []

    def complete(*args: object) -> Assistant:
        raise AssertionError("not used")

    def stream(candidate: object, messages: object, tools: object, emit: object) -> Assistant:
        del messages, tools
        calls.append(getattr(candidate, "provider_id"))
        emit("visible")  # type: ignore[operator]
        raise RuntimeError("lost")

    gateway = AgentProviderGatewaySession(chain, SingleCandidateAgentTransport(complete, stream))
    surface, invocation, _binding = _authorize_gateway_surface(surface, gateway)
    with pytest.raises(RuntimeError, match="lost"):
        gateway.stream(surface, lambda _value: None, invocation_identity=invocation)
    assert calls == ["a"]


def test_gateway_deferred_stream_discards_failed_candidate_deltas_before_fallback() -> None:
    chain = FrozenProviderExecutionChain.freeze(
        [
            AIProviderProfile(id="a", api_key="a", base_url="https://a.test/v1"),
            AIProviderProfile(id="b", api_key="b", base_url="https://b.test/v1"),
        ]
    )
    signals = ToolSelectionSignals(page_kind="offers", current_request="offer")
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-deferred",
            contributors(),
            (),
            signals,
            tuple(candidate.budget() for candidate in chain.candidates),
            _selection_for(signals),
        )
    )
    calls: list[str] = []

    def complete(*_args: object) -> Assistant:
        raise AssertionError("not used")

    def stream(candidate: object, _messages: object, _tools: object, emit: object) -> Assistant:
        provider_id = getattr(candidate, "provider_id")
        calls.append(provider_id)
        assert callable(emit)
        if provider_id == "a":
            emit("a-partial")
            raise RuntimeError("a-lost")
        emit("b-final")
        return Assistant(content="winner")

    gateway = AgentProviderGatewaySession(chain, SingleCandidateAgentTransport(complete, stream))
    surface, invocation, _binding = _authorize_gateway_surface(surface, gateway)
    deltas: list[str] = []

    response = gateway.stream_deferred(surface, deltas.append, invocation_identity=invocation)

    assert response.response.content == "winner"
    assert calls == ["a", "b"]
    assert deltas == ["b-final"]


def test_gateway_deferred_stream_callback_failure_does_not_retry_completed_provider() -> None:
    chain = FrozenProviderExecutionChain.freeze(
        [
            AIProviderProfile(id="a", api_key="a", base_url="https://a.test/v1"),
            AIProviderProfile(id="b", api_key="b", base_url="https://b.test/v1"),
        ]
    )
    signals = ToolSelectionSignals(page_kind="offers", current_request="offer")
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-deferred-sink",
            contributors(),
            (),
            signals,
            tuple(candidate.budget() for candidate in chain.candidates),
            _selection_for(signals),
        )
    )
    calls: list[str] = []

    def complete(*_args: object) -> Assistant:
        raise AssertionError("not used")

    def stream(candidate: object, _messages: object, _tools: object, emit: object) -> Assistant:
        calls.append(getattr(candidate, "provider_id"))
        assert callable(emit)
        emit("deferred")
        return Assistant(content="ok")

    gateway = AgentProviderGatewaySession(chain, SingleCandidateAgentTransport(complete, stream))
    surface, invocation, _binding = _authorize_gateway_surface(surface, gateway)

    def failing_sink(_value: str) -> None:
        raise RuntimeError("sink-failed")

    with pytest.raises(RuntimeError, match="sink-failed"):
        gateway.stream_deferred(surface, failing_sink, invocation_identity=invocation)

    assert calls == ["a"]


@pytest.mark.parametrize("mode", ["complete", "stream", "deferred"])
def test_gateway_checks_active_before_each_fallback_attempt(mode: str) -> None:
    chain = FrozenProviderExecutionChain.freeze(
        [
            AIProviderProfile(id="a", api_key="a", base_url="https://a.test/v1"),
            AIProviderProfile(id="b", api_key="b", base_url="https://b.test/v1"),
        ]
    )
    signals = ToolSelectionSignals(page_kind="offers", current_request="offer")
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-active",
            contributors(),
            (),
            signals,
            tuple(candidate.budget() for candidate in chain.candidates),
            _selection_for(signals),
        )
    )
    calls: list[str] = []
    active = True
    checks: list[int] = []

    def require_active() -> None:
        checks.append(len(calls))
        if not active:
            raise RuntimeCancelled("delivery owner fenced")

    def complete(candidate: object, *_args: object) -> Assistant:
        nonlocal active
        calls.append(getattr(candidate, "provider_id"))
        active = False
        raise RuntimeError("provider-lost")

    def stream(candidate: object, *_args: object) -> Assistant:
        nonlocal active
        calls.append(getattr(candidate, "provider_id"))
        active = False
        raise RuntimeError("provider-lost")

    gateway = AgentProviderGatewaySession(chain, SingleCandidateAgentTransport(complete, stream))
    surface, invocation, _binding = _authorize_gateway_surface(surface, gateway)
    with pytest.raises(RuntimeCancelled):
        if mode == "complete":
            gateway.complete(
                surface,
                invocation_identity=invocation,
                before_attempt=require_active,
            )
        elif mode == "stream":
            gateway.stream(
                surface,
                lambda _value: None,
                invocation_identity=invocation,
                before_attempt=require_active,
            )
        else:
            gateway.stream_deferred(
                surface,
                lambda _value: None,
                invocation_identity=invocation,
                before_attempt=require_active,
            )

    assert calls == ["a"]
    assert checks == [0, 1]
    assert gateway._attempts == set()


@pytest.mark.parametrize("mode", ["complete", "stream", "deferred"])
@pytest.mark.parametrize(
    "error_factory",
    [KeyboardInterrupt, SystemExit, asyncio.CancelledError],
)
def test_gateway_discards_attempt_on_raw_base_exception(
    mode: str,
    error_factory: type[BaseException],
) -> None:
    chain = FrozenProviderExecutionChain.freeze(
        [
            AIProviderProfile(id="a", api_key="a", base_url="https://a.test/v1"),
            AIProviderProfile(id="b", api_key="b", base_url="https://b.test/v1"),
        ]
    )
    signals = ToolSelectionSignals(page_kind="offers", current_request="offer")
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-base-exception",
            contributors(),
            (),
            signals,
            tuple(candidate.budget() for candidate in chain.candidates),
            _selection_for(signals),
        )
    )
    calls: list[str] = []
    error = error_factory("provider-base-exception")

    def complete(candidate: object, *_args: object) -> Assistant:
        calls.append(getattr(candidate, "provider_id"))
        raise error

    def stream(candidate: object, *_args: object) -> Assistant:
        calls.append(getattr(candidate, "provider_id"))
        raise error

    gateway = AgentProviderGatewaySession(chain, SingleCandidateAgentTransport(complete, stream))
    surface, invocation, _binding = _authorize_gateway_surface(surface, gateway)
    with pytest.raises(type(error)) as raised:
        if mode == "complete":
            gateway.complete(surface, invocation_identity=invocation)
        elif mode == "stream":
            gateway.stream(surface, lambda _value: None, invocation_identity=invocation)
        else:
            gateway.stream_deferred(surface, lambda _value: None, invocation_identity=invocation)

    assert raised.value is error
    assert calls == ["a"]
    assert gateway._attempts == set()


@pytest.mark.parametrize("mode", ["stream", "deferred"])
@pytest.mark.parametrize(
    "error_factory",
    [KeyboardInterrupt, SystemExit, asyncio.CancelledError],
)
def test_gateway_discards_attempt_on_raw_sink_base_exception(
    mode: str,
    error_factory: type[BaseException],
) -> None:
    chain = FrozenProviderExecutionChain.freeze(
        [
            AIProviderProfile(id="a", api_key="a", base_url="https://a.test/v1"),
            AIProviderProfile(id="b", api_key="b", base_url="https://b.test/v1"),
        ]
    )
    signals = ToolSelectionSignals(page_kind="offers", current_request="offer")
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-sink-base-exception",
            contributors(),
            (),
            signals,
            tuple(candidate.budget() for candidate in chain.candidates),
            _selection_for(signals),
        )
    )
    calls: list[str] = []
    error = error_factory("sink-base-exception")

    def complete(*_args: object) -> Assistant:
        raise AssertionError("not used")

    def stream(candidate: object, _messages: object, _tools: object, emit: object) -> Assistant:
        calls.append(getattr(candidate, "provider_id"))
        assert callable(emit)
        emit("partial")
        return Assistant(content="ok")

    gateway = AgentProviderGatewaySession(chain, SingleCandidateAgentTransport(complete, stream))
    surface, invocation, _binding = _authorize_gateway_surface(surface, gateway)

    def failing_sink(_value: str) -> None:
        raise error

    with pytest.raises(type(error)) as raised:
        if mode == "stream":
            gateway.stream(surface, failing_sink, invocation_identity=invocation)
        else:
            gateway.stream_deferred(surface, failing_sink, invocation_identity=invocation)

    assert raised.value is error
    assert calls == ["a"]
    assert gateway._attempts == set()


@pytest.mark.parametrize(
    "control_error",
    [RuntimeCancelled(), RuntimeTransportAborted(), RuntimeAgentTimedOut()],
)
@pytest.mark.parametrize("mode", ["complete", "stream"])
def test_gateway_never_falls_back_after_runtime_control_error(
    control_error: Exception,
    mode: str,
) -> None:
    chain = FrozenProviderExecutionChain.freeze(
        [
            AIProviderProfile(id="a", api_key="a", base_url="https://a.test/v1"),
            AIProviderProfile(id="b", api_key="b", base_url="https://b.test/v1"),
        ]
    )
    signals = ToolSelectionSignals(page_kind="offers", current_request="offer")
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-control",
            contributors(),
            (),
            signals,
            tuple(candidate.budget() for candidate in chain.candidates),
            _selection_for(signals),
        )
    )
    calls: list[str] = []

    def complete(candidate: object, *_args: object) -> Assistant:
        calls.append(getattr(candidate, "provider_id"))
        raise control_error

    def stream(candidate: object, *_args: object) -> Assistant:
        calls.append(getattr(candidate, "provider_id"))
        raise control_error

    gateway = AgentProviderGatewaySession(
        chain,
        SingleCandidateAgentTransport(complete, stream),
    )
    surface, invocation, _binding = _authorize_gateway_surface(surface, gateway)

    with pytest.raises(type(control_error)) as raised:
        if mode == "complete":
            gateway.complete(surface, invocation_identity=invocation)
        else:
            gateway.stream(surface, lambda _value: None, invocation_identity=invocation)

    assert raised.value is control_error
    assert calls == ["a"]


def test_gateway_attempt_identity_is_session_owned_and_single_use() -> None:
    profile = AIProviderProfile(id="a", api_key="a", base_url="https://a.test/v1")
    chain = FrozenProviderExecutionChain.freeze([profile])
    signals = ToolSelectionSignals(page_kind="offers", current_request="offer")
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-owned",
            contributors(),
            (),
            signals,
            tuple(candidate.budget() for candidate in chain.candidates),
            _selection_for(signals),
        )
    )

    def complete(*_args: object) -> Assistant:
        return Assistant(content="ok")

    def stream(*_args: object) -> Assistant:
        raise AssertionError("not used")

    owner = AgentProviderGatewaySession(
        chain,
        SingleCandidateAgentTransport(complete, stream),
    )
    stranger = AgentProviderGatewaySession(
        chain,
        SingleCandidateAgentTransport(complete, stream),
    )
    surface, invocation, binding = _authorize_gateway_surface(surface, owner)
    response = owner.complete(surface, invocation_identity=invocation)

    with pytest.raises(ProjectionError, match="provider_response_attempt_mismatch"):
        binding.validate_response(response, attempt_validator=stranger.consume_attempt)

    assert (
        binding.validate_response(
            response,
            attempt_validator=owner.consume_attempt,
        ).content
        == "ok"
    )
    with pytest.raises(ProjectionError, match="provider_response_attempt_mismatch"):
        binding.validate_response(response, attempt_validator=owner.consume_attempt)


def test_runtime_signal_sink_is_capacity_one_and_fail_open() -> None:
    sink: RuntimeSignalSink[str] = RuntimeSignalSink()
    assert sink.try_emit("title") == "emitted"
    assert sink.try_emit("other") == "duplicate"
    assert sink.drain() == "title"
    assert sink.try_emit("again") == "duplicate"
    sink.close()
    assert sink.try_emit("closed") == "closed"


def test_loader_uses_one_snapshot_and_fetchmany(tmp_path: Path) -> None:
    database = tmp_path / "context.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
        connection.executemany(
            "INSERT INTO items(value) VALUES (?)", [(str(i),) for i in range(70)]
        )
    loader: ContextSourceLoader[tuple[tuple[object, ...], ...], tuple[str, ...]] = (
        ContextSourceLoader(database)
    )

    def read(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
        return fetch_rows(connection.execute("SELECT value FROM items ORDER BY id"))

    result = loader.load(read, lambda rows: tuple(str(row[0]) for row in rows))
    assert result == tuple(str(i) for i in range(70))


def test_loader_propagates_base_exception(tmp_path: Path) -> None:
    database = tmp_path / "context.db"
    sqlite3.connect(database).close()
    loader: ContextSourceLoader[None, None] = ContextSourceLoader(database)

    def stop(_connection: sqlite3.Connection) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        loader.load(stop, lambda value: value)


def test_manifest_v2_is_canonical_private_and_validated_by_shared_entrypoint() -> None:
    audit = RuntimeSurfaceAudit(
        "model-surface-budget-v1",
        tuple(
            (name, "disabled" if name.endswith("summary") else "ready")
            for name in CONTRIBUTOR_ORDER
        ),  # type: ignore[arg-type]
        ("group-1",),
        MODEL_TOOL_NAMES,
        ("a" * 64,),
        100,
        80,
        20,
        False,
    )
    prepared = prepare_surface_manifest_v2(
        audit,
        key_id="11111111-1111-4111-8111-111111111111",
        secret=b"secret",
        provider_identities=("private-provider/model",),
        provider_view=_selector_bundle().provider_view(),
        signals=("trusted_page",),
    )
    validated = validate_context_manifest_json(prepared.manifest_json)
    assert validated["manifest_schema_version"] == 2
    assert "private-provider" not in prepared.manifest_json
    assert "logical_input_fingerprint" not in prepared.manifest_json


def test_manifest_v2_rejects_65537_bytes() -> None:
    base = {
        "manifest_schema_version": 2,
        "budget_policy_version": "model-surface-budget-v1",
        "providers": ["a" * 64],
        "contributors": [],
        "history_groups": [],
        "tools": [],
        "sources": [],
        "signals": [],
        "counts": {},
        "truncated": False,
        "fingerprint_key_id": "x" * 65_536,
    }
    with pytest.raises(ManifestV2ValidationError, match="64 KiB"):
        validate_surface_manifest_v2(json.dumps(base, separators=(",", ":"), sort_keys=True))


def test_manifest_v2_prepare_rejects_tool_outside_injected_bundle_provider_view() -> None:
    bundle = _selector_bundle()
    audit = RuntimeSurfaceAudit(
        "model-surface-budget-v1",
        tuple((name, "ready") for name in CONTRIBUTOR_ORDER),
        (),
        ("attacker_tool",),
        (),
        1,
        1,
        1,
        False,
    )

    with pytest.raises(ManifestV2ValidationError, match="unapproved tool"):
        prepare_surface_manifest_v2(
            audit,
            key_id="11111111-1111-4111-8111-111111111111",
            secret=b"secret",
            provider_identities=("provider",),
            provider_view=bundle.provider_view(),
        )


def test_manifest_v2_prepare_requires_exact_provider_view_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameter = inspect.signature(prepare_surface_manifest_v2).parameters["provider_view"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    audit = RuntimeSurfaceAudit(
        "model-surface-budget-v1",
        tuple((name, "ready") for name in CONTRIBUTOR_ORDER),
        (),
        (MODEL_TOOL_NAMES[0],),
        (),
        1,
        1,
        1,
        False,
    )

    def forbidden_build(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid Provider view reached Manifest projection")

    monkeypatch.setattr(manifest_module, "_build_manifest_payload", forbidden_build)
    with pytest.raises(ManifestV2ValidationError, match="Provider metadata view"):
        prepare_surface_manifest_v2(
            audit,
            key_id="11111111-1111-4111-8111-111111111111",
            secret=b"secret",
            provider_identities=("provider",),
            provider_view=None,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("field", ["tools", "signals"])
def test_manifest_v2_safely_rejects_non_string_set_members(field: str) -> None:
    audit = RuntimeSurfaceAudit(
        "model-surface-budget-v1",
        tuple((name, "ready") for name in CONTRIBUTOR_ORDER),
        (),
        MODEL_TOOL_NAMES,
        (),
        1,
        1,
        1,
        False,
    )
    prepared = prepare_surface_manifest_v2(
        audit,
        key_id="11111111-1111-4111-8111-111111111111",
        secret=b"secret",
        provider_identities=("provider",),
        provider_view=_selector_bundle().provider_view(),
        signals=("trusted_page",),
    )
    manifest = json.loads(prepared.manifest_json)
    manifest[field] = [[]]
    malformed = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    with pytest.raises(ManifestV2ValidationError):
        validate_surface_manifest_v2(malformed)
    with pytest.raises(JournalEventValidationError):
        validate_context_manifest_json(malformed)


def test_manifest_v2_safely_rejects_non_string_contributor_status() -> None:
    audit = RuntimeSurfaceAudit(
        "model-surface-budget-v1",
        tuple((name, "ready") for name in CONTRIBUTOR_ORDER),
        (),
        MODEL_TOOL_NAMES,
        (),
        1,
        1,
        1,
        False,
    )
    prepared = prepare_surface_manifest_v2(
        audit,
        key_id="11111111-1111-4111-8111-111111111111",
        secret=b"secret",
        provider_identities=("provider",),
        provider_view=_selector_bundle().provider_view(),
    )
    manifest = json.loads(prepared.manifest_json)
    manifest["contributors"][0]["status"] = []
    malformed = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    with pytest.raises(ManifestV2ValidationError):
        validate_surface_manifest_v2(malformed)
    with pytest.raises(JournalEventValidationError):
        validate_context_manifest_json(malformed)


def test_maximal_semantic_manifest_reaches_every_array_limit_under_cap() -> None:
    sources = tuple(
        RuntimeSourceAudit(
            f"source_{source_index}",
            f"revision:{source_index}",
            f"{source_index:064x}",
            tuple(
                SourceChunk(
                    f"$.field_{chunk_index}",
                    chunk_index + 1,
                    8,
                    "",
                    False,
                    100,
                    50,
                )
                for chunk_index in range(8)
            ),
        )
        for source_index in range(8)
    )
    audit = RuntimeSurfaceAudit(
        "model-surface-budget-v1",
        tuple((name, "ready") for name in CONTRIBUTOR_ORDER),
        tuple(f"group-{index}" for index in range(32)),
        MODEL_TOOL_NAMES,
        tuple(source.content_revision_fingerprint for source in sources),
        100,
        80,
        20,
        True,
        sources,
    )
    prepared = prepare_surface_manifest_v2(
        audit,
        key_id="11111111-1111-4111-8111-111111111111",
        secret=b"k" * 32,
        provider_identities=tuple(f"provider-{index}" for index in range(8)),
        provider_view=_selector_bundle().provider_view(),
        signals=MANIFEST_SIGNAL_VALUES,
    )
    manifest = validate_surface_manifest_v2(prepared.manifest_json)
    assert len(prepared.manifest_json.encode("utf-8")) < 65_536
    assert len(manifest["providers"]) == 8
    assert len(manifest["contributors"]) == 10
    assert len(manifest["history_groups"]) == 32
    assert len(manifest["tools"]) == 25
    assert len(manifest["sources"]) == 8
    assert sum(len(source["chunks"]) for source in manifest["sources"]) == 64
    assert len(manifest["signals"]) == 32


def test_manifest_v2_budget_guard_interrupts_maximal_audit_at_exact_checkpoint() -> None:
    sources = tuple(
        RuntimeSourceAudit(
            f"source_{source_index}",
            f"revision:{source_index}",
            f"{source_index:064x}",
            tuple(
                SourceChunk(
                    f"$.field_{chunk_index}",
                    chunk_index + 1,
                    8,
                    "",
                    False,
                    100,
                    50,
                )
                for chunk_index in range(8)
            ),
        )
        for source_index in range(8)
    )
    audit = RuntimeSurfaceAudit(
        "model-surface-budget-v1",
        tuple((name, "ready") for name in CONTRIBUTOR_ORDER),
        tuple(f"group-{index}" for index in range(32)),
        MODEL_TOOL_NAMES,
        tuple(source.content_revision_fingerprint for source in sources),
        100,
        80,
        20,
        True,
        sources,
    )
    guard = FailingGuard(fail_at=12)

    with pytest.raises(JournalBudgetExhausted):
        prepare_surface_manifest_v2(
            audit,
            key_id="11111111-1111-4111-8111-111111111111",
            secret=b"k" * 32,
            provider_identities=tuple(f"provider-{index}" for index in range(8)),
            provider_view=_selector_bundle().provider_view(),
            signals=MANIFEST_SIGNAL_VALUES,
            budget_check=guard,
        )

    assert guard.calls == 12


@pytest.mark.parametrize(
    "domain",
    [
        b"offerpilot-surface-provider-v2",
        b"offerpilot-surface-history-v2",
        b"offerpilot-surface-source-v2",
        b"offerpilot-surface-chunk-v2",
    ],
)
@pytest.mark.parametrize("fail_at", [5, 8])
def test_manifest_identity_budget_guard_checks_bounded_utf8_chunks(
    domain: bytes,
    fail_at: int,
) -> None:
    value = "界" * 5000
    expected = hmac.new(
        b"k" * 32,
        domain + b"\0" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert _identity(b"k" * 32, domain, value) == expected

    guard = FailingGuard(fail_at=fail_at)

    with pytest.raises(JournalBudgetExhausted):
        _identity(
            b"k" * 32,
            domain,
            value,
            budget_check=guard,
        )

    assert guard.calls == fail_at


def test_manifest_v2_sha_updates_fixed_byte_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = tuple(
        RuntimeSourceAudit(
            f"source_{source_index}",
            f"revision:{source_index}",
            "a" * 64,
            tuple(
                SourceChunk(
                    f"$.field_{chunk_index}",
                    chunk_index + 1,
                    32,
                    "",
                    False,
                    100,
                    50,
                )
                for chunk_index in range(32)
            ),
        )
        for source_index in range(2)
    )
    audit = RuntimeSurfaceAudit(
        "model-surface-budget-v1",
        tuple((name, "ready") for name in CONTRIBUTOR_ORDER),
        tuple(f"group-{index}" for index in range(32)),
        MODEL_TOOL_NAMES,
        tuple(source.content_revision_fingerprint for source in sources),
        100,
        80,
        20,
        True,
        sources,
    )
    chunks: list[bytes] = []
    monkeypatch.setattr(
        manifest_module,
        "hashlib",
        SimpleNamespace(sha256=_recording_sha256(chunks)),
    )

    prepared = prepare_surface_manifest_v2(
        audit,
        key_id="11111111-1111-4111-8111-111111111111",
        secret=b"k" * 32,
        provider_identities=tuple(f"provider-{index}" for index in range(8)),
        provider_view=_selector_bundle().provider_view(),
        signals=MANIFEST_SIGNAL_VALUES,
    )

    assert len(prepared.manifest_json.encode("utf-8")) > 4096
    assert (
        prepared.manifest_digest
        == hashlib.sha256(prepared.manifest_json.encode("utf-8")).hexdigest()
    )
    assert chunks
    assert max(len(chunk) for chunk in chunks) <= 4096
    assert any(len(chunk) == 4096 for chunk in chunks)


@pytest.mark.parametrize("fail_at", [175, 176, 177])
def test_manifest_budget_guard_reaches_source_chunk_validation_and_sha_phases(
    fail_at: int,
) -> None:
    large_identity = "界" * 5000
    source = RuntimeSourceAudit(
        "source",
        large_identity,
        "a" * 64,
        (SourceChunk(large_identity, 1, 1, "", False, 100, 50),),
    )
    audit = RuntimeSurfaceAudit(
        "model-surface-budget-v1",
        tuple((name, "ready") for name in CONTRIBUTOR_ORDER),
        (large_identity,),
        (MODEL_TOOL_NAMES[0],),
        (source.content_revision_fingerprint,),
        100,
        80,
        20,
        False,
        (source,),
    )
    guard = FailingGuard(fail_at=fail_at)

    with pytest.raises(JournalBudgetExhausted):
        prepare_surface_manifest_v2(
            audit,
            key_id="11111111-1111-4111-8111-111111111111",
            secret=b"k" * 32,
            provider_identities=(large_identity,),
            provider_view=_selector_bundle().provider_view(),
            budget_check=guard,
        )

    assert guard.calls == fail_at


def test_migration_0027_records_and_database_accepts_v2_limit(tmp_path: Path) -> None:
    factory = init_database(tmp_path / "offerpilot.db")
    with factory() as session:
        version = session.scalar(
            text(
                "SELECT version FROM schema_migrations "
                "WHERE version = '0027_context_projector_manifest_v2'"
            )
        )
        sql = session.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'agent_context_snapshots'"
            )
        )
        conversation = Conversation(title="manifest-boundary")
        session.add(conversation)
        session.flush()
        run = AgentRun(
            id="11111111-1111-4111-8111-111111111111",
            conversation_id=conversation.id,
            origin_kind="user_message",
            initial_context_type="workspace",
            fingerprint_key_id="22222222-2222-4222-8222-222222222222",
            initial_transport_mode="sync",
            initial_route_kind="model",
            status="running",
        )
        session.add(run)
        session.flush()
        session.add(
            AgentContextSnapshot(
                id="33333333-3333-4333-8333-333333333333",
                run_id=run.id,
                execution_segment_id="44444444-4444-4444-8444-444444444444",
                snapshot_key="model-input:65536",
                manifest_schema_version=2,
                snapshot_kind="model_input",
                manifest_json="x" * 65_536,
                manifest_digest="a" * 64,
                canonicalizer_version="2",
                logical_input_fingerprint="b" * 64,
                fingerprint_key_id="22222222-2222-4222-8222-222222222222",
            )
        )
        session.commit()
        session.add(
            AgentContextSnapshot(
                id="55555555-5555-4555-8555-555555555555",
                run_id=run.id,
                execution_segment_id="44444444-4444-4444-8444-444444444444",
                snapshot_key="model-input:65537",
                manifest_schema_version=2,
                snapshot_kind="model_input",
                manifest_json="x" * 65_537,
                manifest_digest="c" * 64,
                canonicalizer_version="2",
                logical_input_fingerprint="d" * 64,
                fingerprint_key_id="22222222-2222-4222-8222-222222222222",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
    assert version == "0027_context_projector_manifest_v2"
    assert "65536" in str(sql)


def test_real_chat_adapter_uses_projected_surface_and_persists_v2_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import offerpilot.ai.client as ai_client

    save_config(tmp_path, Config(api_key="sk-test", confirmation_secret="secret"))
    requests: list[dict[str, object]] = []

    def completion(**payload: object) -> dict[str, object]:
        requests.append(payload)
        return {"choices": [{"message": {"content": "已完成", "tool_calls": []}}]}

    monkeypatch.setattr(ai_client, "completion", completion)
    with TestClient(
        create_app(
            data_dir=tmp_path,
            run_recorder_factory=_projector_journal_factory(tmp_path),
        )
    ) as client:
        response = client.post("/api/chat", json={"message": "请比较 offer"})
        assert response.status_code == 200
    assert requests
    assert 0 < len(requests[0].get("tools", [])) < 25  # type: ignore[arg-type]
    runs, events, snapshots = _wait_for_projector_journal_completion(tmp_path)
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert runs[0].recording_status == "healthy"
    assert events[-1].event_type == "segment.finished"
    snapshots = [snapshot for snapshot in snapshots if snapshot.snapshot_kind == "model_input"]
    assert snapshots
    assert snapshots[0].manifest_schema_version == 2
    manifest = validate_surface_manifest_v2(snapshots[0].manifest_json)
    assert manifest["tools"]
