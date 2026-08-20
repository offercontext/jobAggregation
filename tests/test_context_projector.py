from __future__ import annotations

import json
import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from fastapi.testclient import TestClient

from offerpilot.agent_runtime.events import (
    JournalEventValidationError,
    validate_context_manifest_json,
)
from offerpilot.agent_runtime.budget import JournalBudgetExhausted
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG, MODEL_TOOL_NAMES
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
    prepare_surface_manifest_v2,
    validate_surface_manifest_v2,
)
from offerpilot.context_projector.projector import ModelSurfaceProjector, ProjectionRequest
from offerpilot.context_projector.selector import ToolSelectionSignals, select_tools
from offerpilot.context_projector.signals import RuntimeSignalSink
from offerpilot.config import AIProviderProfile, Config, save_config
from offerpilot.db import init_database
from offerpilot.models import AgentContextSnapshot, AgentRun, Conversation
from offerpilot.api import create_app


class FailingGuard:
    def __init__(self, fail_at: int) -> None:
        self.fail_at = fail_at
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls == self.fail_at:
            raise JournalBudgetExhausted


def frozen(role: str, content: str = "", *, message_id: int = 0) -> FrozenMessage:
    return FrozenMessage.freeze(Message(role=role, content=content), source_message_id=message_id)


def contributors(request: str = "比较 offer") -> tuple[ContributorResult, ...]:
    values = []
    for name in CONTRIBUTOR_ORDER:
        status = "disabled" if name in {
            "confirmed_memory",
            "knowledge_context",
            "older_conversation_summary",
        } else "not_applicable"
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
    source = FrozenSource.present(kind="application", revision_identity="revision:7", content={"x": 1})
    changed = FrozenSource.present(kind="application", revision_identity="revision:7", content={"x": 2})
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
    selection = select_tools(
        MODEL_TOOL_CATALOG.provider_contracts(),
        ToolSelectionSignals(page_kind="offers", current_request="比较薪资"),
    )
    assert selection.names == tuple(name for name in MODEL_TOOL_NAMES if name in selection.names)
    assert {"list_offers", "get_offer", "compare_offers"}.issubset(selection.names)
    assert len(selection.envelope_fingerprint) == 64


def test_tool_selector_falls_back_to_all_typed_tools_and_fails_on_bad_signal() -> None:
    selection = select_tools(
        MODEL_TOOL_CATALOG.provider_contracts(), ToolSelectionSignals(page_kind="workspace")
    )
    assert selection.names == MODEL_TOOL_NAMES
    assert selection.fallback_all is True
    with pytest.raises(ProjectionError, match="unknown_page_kind"):
        select_tools(
            MODEL_TOOL_CATALOG.provider_contracts(), ToolSelectionSignals(page_kind="evil")
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
            FrozenMessage.freeze(
                Message("tool", "[]", tool_call_id="large"), source_message_id=3
            ),
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
    chunks = chunk_structured_source(
        {"resume": {"summary": "求" * 100}}, byte_cap=64, max_chunks=2
    )
    assert len(chunks) == 2
    assert chunks[0].path == "$.resume.summary"
    assert chunks[0].original_bytes == 300
    assert chunks[0].original_codepoints == 100
    assert chunks[-1].truncated is True


def test_projection_is_repeatable_and_preserves_current_request() -> None:
    request = ProjectionRequest(
        model_call_id="call-1",
        contributors=contributors(),
        history=(frozen("user", "old", message_id=1), frozen("assistant", "answer", message_id=2)),
        provider_tools=MODEL_TOOL_CATALOG.provider_contracts(),
        tool_signals=ToolSelectionSignals(page_kind="offers", current_request="比较 offer"),
        provider_budgets=(ProviderBudget(),),
    )
    first = ModelSurfaceProjector().project(request)
    second = ModelSurfaceProjector().project(request)
    assert first.runtime_surface_fingerprint == second.runtime_surface_fingerprint
    assert first.messages[-1].content == "比较 offer"
    assert first.audit.selected_tool_names


def test_projection_mandatory_overflow_fails_before_provider() -> None:
    request = ProjectionRequest(
        model_call_id="call-1",
        contributors=contributors("x" * 40_000),
        history=(),
        provider_tools=MODEL_TOOL_CATALOG.provider_contracts(),
        tool_signals=ToolSelectionSignals(current_request="offer"),
        provider_budgets=(ProviderBudget(context_window=10_000),),
    )
    with pytest.raises(ProjectionError, match="mandatory_surface_over_budget"):
        ModelSurfaceProjector().project(request)


def test_bound_response_rejects_unexposed_tool_without_executor() -> None:
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-1",
            contributors(),
            (),
            MODEL_TOOL_CATALOG.provider_contracts(),
            ToolSelectionSignals(page_kind="offers", current_request="offer"),
            (ProviderBudget(),),
        )
    )
    binding = ModelCallSurfaceBinding.from_surface(surface)
    response = BoundProviderResponse(
        "call-1",
        0,
        "attempt",
        surface.runtime_surface_fingerprint,
        Assistant(tool_calls=[ToolCall("x", "delete_note", "{}")]),
    )
    with pytest.raises(ProjectionError, match="unknown_tool"):
        binding.validate_response(response)


def test_gateway_reuses_surface_and_stops_stream_fallback_after_delta() -> None:
    profiles = [
        AIProviderProfile(id="a", api_key="a", base_url="https://a.test/v1"),
        AIProviderProfile(id="b", api_key="b", base_url="https://b.test/v1"),
    ]
    chain = FrozenProviderExecutionChain.freeze(profiles)
    surface = ModelSurfaceProjector().project(
        ProjectionRequest(
            "call-1",
            contributors(),
            (),
            MODEL_TOOL_CATALOG.provider_contracts(),
            ToolSelectionSignals(page_kind="offers", current_request="offer"),
            tuple(candidate.budget() for candidate in chain.candidates),
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
    with pytest.raises(RuntimeError, match="lost"):
        gateway.stream(surface, lambda _value: None)
    assert calls == ["a"]


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
        connection.executemany("INSERT INTO items(value) VALUES (?)", [(str(i),) for i in range(70)])
    loader: ContextSourceLoader[tuple[tuple[object, ...], ...], tuple[str, ...]] = ContextSourceLoader(database)

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
        tuple((name, "disabled" if name.endswith("summary") else "ready") for name in CONTRIBUTOR_ORDER),  # type: ignore[arg-type]
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
            signals=MANIFEST_SIGNAL_VALUES,
            budget_check=guard,
        )

    assert guard.calls == 12


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
    with TestClient(create_app(data_dir=tmp_path)) as client:
        response = client.post("/api/chat", json={"message": "请比较 offer"})
        assert response.status_code == 200
    assert requests
    assert 0 < len(requests[0].get("tools", [])) < 25  # type: ignore[arg-type]
    factory = init_database(tmp_path / "data.db")
    with factory() as session:
        snapshots = session.query(AgentContextSnapshot).filter_by(snapshot_kind="model_input").all()
    assert snapshots
    assert snapshots[0].manifest_schema_version == 2
    manifest = validate_surface_manifest_v2(snapshots[0].manifest_json)
    assert manifest["tools"]
