from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

import pytest
from sqlalchemy import text

from offerpilot.ai.tool_runtime.catalog import ToolCatalog, compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.legacy import LegacyRouteSourceV1
from offerpilot.ai.tool_runtime.legacy_proof import (
    LegacyApprovedConfirmationInput,
    LegacyConfirmationLookupIdentity,
)
from offerpilot.ai.tool_runtime.metadata import OperationRouteIdentityV1, ToolMetadataBundleV1
from offerpilot.ai.tool_specs import legacy as legacy_specs
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.write_operations import (
    WriteOperationCoordinator,
    WriteOperationError,
    WriteOperationRepository,
    ledger_fingerprint,
    load_or_create_ledger_key,
)
from offerpilot.db import init_database
from offerpilot.models import Conversation
from offerpilot.pilot_runtime.legacy_route import build_legacy_pending_identity_verifier_port
from offerpilot.pilot_runtime.service import RuntimeDependencies


ROOT = Path(__file__).parents[2]
PRODUCTION_ROOT = ROOT / "src" / "offerpilot"
ORDERED_LEGACY_NAMES = (
    "save_application_jd_version",
    "create_application_submission_snapshot",
    "record_application_outcome",
)
ROUTE_SOURCES = (
    "jd_clarification",
    "jd_deterministic_action",
    "submission_snapshot_action",
    "outcome_recording_action",
)


def _composition_module() -> Any:
    return importlib.import_module("offerpilot.pilot_runtime.composition")


def _production_factory() -> Callable[..., Any]:
    factory = getattr(
        _composition_module(),
        "build_production_tool_metadata_components",
        None,
    )
    assert callable(factory), (
        "Task 9 must expose the single production Tool Metadata component factory"
    )
    return factory


def _pending_verifier() -> Any:
    route_module = importlib.import_module("offerpilot.pilot_runtime.legacy_route")
    builder = getattr(route_module, "build_legacy_pending_identity_verifier_port", None)
    assert callable(builder), "Task 8 must expose the Legacy verifier Port builder"
    return builder(
        backend=SimpleNamespace(
            read_snapshot=lambda *_args, **_kwargs: {},
            locked_recheck=lambda *_args, **_kwargs: {},
            claim_cas=lambda *_args, **_kwargs: {},
        ),
        ledger_key=SimpleNamespace(
            key_id="00000000-0000-0000-0000-000000000001",
            secret=b"k" * 32,
        ),
    )


def _production_components() -> Any:
    return _production_factory()(pending_identity_verifier_port=_pending_verifier())


def _second_exact_typed_catalog() -> ToolCatalog:
    return ToolCatalog(
        MODEL_TOOL_CATALOG.specs,
        expected_names=tuple(spec.name for spec in MODEL_TOOL_CATALOG.specs),
        authority_manifest=MODEL_TOOL_CATALOG.authority_manifest,
    )


def _minimal_runtime(tmp_path: Path, *, catalog: ToolCatalog) -> Any:
    runtime, _repository = _minimal_runtime_with_repository(tmp_path, catalog=catalog)
    return runtime


def _minimal_runtime_with_repository(
    tmp_path: Path,
    *,
    catalog: ToolCatalog,
) -> tuple[Any, WriteOperationRepository]:
    composition = _composition_module()
    dependency = object()
    sessions = init_database(tmp_path / "minimal-runtime.sqlite3")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    runtime = composition.build_pilot_runtime(
        data_dir=tmp_path,
        chat=dependency,
        applications=dependency,
        application_jd_versions=dependency,
        application_outcomes=dependency,
        events=dependency,
        notes=dependency,
        offers=dependency,
        resumes=dependency,
        jd_analyses=dependency,
        context_source_loader=dependency,
        run_recorder_factory=None,
        chat_model=None,
        write_operations=repository,
        write_coordinator=WriteOperationCoordinator(repository),
        source_loader=lambda *_args, **_kwargs: dependency,
        system_message=lambda: dependency,
        clarification_message=lambda *_args, **_kwargs: None,
        page_context_messages=lambda _page: (),
        catalog=catalog,
    )
    return runtime, repository


def _proof_ready_production_components(tmp_path: Path) -> tuple[Any, Any]:
    proof_fixtures = importlib.import_module("tests.tool_metadata.test_legacy_confirmation_proof")
    sessions = init_database(tmp_path / "production-proof.sqlite3")
    key = load_or_create_ledger_key(tmp_path, sessions)
    backend_type = getattr(proof_fixtures, "_VerifierBackend")
    backend = backend_type(key)
    backend.session_factory = sessions
    verifier = build_legacy_pending_identity_verifier_port(
        backend=backend,
        ledger_key=key,
    )
    components = _production_factory()(pending_identity_verifier_port=verifier)
    return components, backend


def _actual_legacy_projection(components: Any) -> dict[str, object]:
    initial = components.initial_routes
    catalog = initial.catalog
    adapters = catalog.ordered_adapters
    owner = initial.owner_lease_factory.open()
    routes: list[dict[str, object]] = []
    try:
        for adapter in adapters:
            for source in adapter.initial_route_sources:
                issuer = initial.initial_issuer_for(source)
                lease = issuer.open_request_lease(owner)
                token = issuer.issue(lease)
                handle = initial.initial_route_port.resolve_initial(token)
                binding = initial.initial_route_port.require_route(handle)
                routes.append(
                    {
                        "route_source": source.value,
                        "adapter_ordinal": binding.ordinal,
                    }
                )
    finally:
        owner.close()
    return {
        "boundary_version": "legacy-deterministic-boundary-v1",
        "provider_visibility": "forbidden",
        "adapter_kind": "legacy_deterministic",
        "ordered_names": [adapter.name for adapter in adapters],
        "chained_policies": [adapter.chained_policy for adapter in adapters],
        "initial_route_bindings": routes,
    }


def test_production_components_publish_one_complete_25_3_4_bundle_graph() -> None:
    components = _production_components()
    bundle = components.bundle
    assert type(bundle) is ToolMetadataBundleV1

    views = (
        bundle.provider_view(),
        bundle.discovery_view(),
        bundle.authority_view(),
        bundle.operation_view(),
        bundle.legacy_boundary(),
        bundle.compensation_view(),
    )
    token = bundle.bundle_instance_token
    assert len(views) == 6
    assert all(view.bundle_instance_token is token for view in views)
    assert len(bundle.provider_view().ordered_contracts) == 25
    assert len(bundle.discovery_view().ordered_entries) == 25
    assert len(bundle.authority_view().entries) == 25
    assert len(bundle.operation_view().entries) == 25
    assert (
        tuple(binding.name for binding in bundle.legacy_boundary().ordered_adapter_bindings)
        == ORDERED_LEGACY_NAMES
    )
    assert len(bundle.compensation_view().ordered_handler_bindings) == 4

    initial = components.initial_routes
    confirmation = components.confirmation_routes
    operation_port = components.operation_port
    compensation_registry = components.compensation_registry
    assert initial.catalog is not confirmation.catalog
    assert tuple(adapter.name for adapter in initial.catalog.ordered_adapters) == (
        ORDERED_LEGACY_NAMES
    )
    assert initial.initial_route_port.bundle_instance_token is token
    assert confirmation.bundle_instance_token is token
    assert confirmation.catalog.bundle_instance_token is token
    assert confirmation.proof_consumer_port.bundle_instance_token is token
    assert confirmation.proof_issuer.bundle_instance_token is token
    assert operation_port.bundle_instance_token is token
    assert compensation_registry.bundle_instance_token is token
    assert compensation_registry.compensation_view is bundle.compensation_view()
    assert (
        operation_port.legacy_route_registry_token
        is components._legacy_route_verifier.registry_token
    )
    assert (
        operation_port.legacy_route_registry_token is not initial.initial_route_port.registry_token
    )
    assert operation_port.compensation_registry_token is compensation_registry.registry_token
    assert len(operation_port.typed_primary_entries) == 25
    assert len(operation_port.legacy_primary_entries) == 3
    assert len(operation_port.compensation_entries) == 4
    assert len(operation_port.required_undo_entries) == 4

    for binding in bundle.compensation_view().ordered_handler_bindings:
        handler = compensation_registry.bind_handler(binding)
        assert compensation_registry.require_handler_handle(handler) is binding


def test_build_pilot_runtime_rejects_a_second_exact_typed_catalog(
    tmp_path: Path,
) -> None:
    second_catalog = _second_exact_typed_catalog()
    assert second_catalog is not MODEL_TOOL_CATALOG
    assert all(
        duplicate is production
        for duplicate, production in zip(
            second_catalog.provider_contracts(),
            MODEL_TOOL_CATALOG.provider_contracts(),
        )
    )

    with pytest.raises(ValueError, match="Catalog|catalog|Bundle"):
        _minimal_runtime(tmp_path, catalog=second_catalog)


def test_runtime_dependencies_reject_catalog_outside_the_exact_bundle() -> None:
    components = _production_components()
    bundle = components.bundle

    with pytest.raises(ValueError, match="Catalog|catalog|Bundle"):
        RuntimeDependencies(
            catalog=_second_exact_typed_catalog(),
            metadata_bundle=bundle,
            metadata_components=components,
            provider_metadata_view=bundle.provider_view(),
            discovery_metadata_view=bundle.discovery_view(),
            authority_metadata_view=bundle.authority_view(),
        )


def test_build_pilot_runtime_publishes_the_bundle_typed_catalog_identity(
    tmp_path: Path,
) -> None:
    runtime = _minimal_runtime(tmp_path, catalog=MODEL_TOOL_CATALOG)

    assert runtime._dependencies.catalog is runtime.metadata_bundle._typed_catalog


def test_build_pilot_runtime_wires_real_legacy_verifier_to_repository_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _composition_module()
    original = composition.build_legacy_pending_identity_verifier_port
    calls: list[tuple[object, object]] = []

    def capture(*, backend: object, ledger_key: object) -> object:
        calls.append((backend, ledger_key))
        return original(backend=backend, ledger_key=ledger_key)

    monkeypatch.setattr(
        composition,
        "build_legacy_pending_identity_verifier_port",
        capture,
    )
    runtime, repository = _minimal_runtime_with_repository(
        tmp_path,
        catalog=MODEL_TOOL_CATALOG,
    )

    assert len(calls) == 1
    backend, ledger_key = calls[0]
    assert ledger_key is repository.key
    assert "Unavailable" not in type(backend).__name__
    assert callable(getattr(backend, "read_snapshot", None))
    assert callable(getattr(backend, "locked_recheck", None))
    assert callable(getattr(backend, "claim_cas", None))
    assert runtime._dependencies.metadata_components is not None


def test_production_composition_has_no_unavailable_or_synthetic_legacy_verifier() -> None:
    source = (PRODUCTION_ROOT / "pilot_runtime" / "composition.py").read_text(encoding="utf-8")

    assert "_UnavailableLegacyPendingIdentityBackend" not in source
    assert 'secret=b"\\x00" * 32' not in source


def test_production_legacy_verifier_backend_reads_locks_and_claims_real_rows(
    tmp_path: Path,
) -> None:
    sessions = init_database(tmp_path / "legacy-verifier.sqlite3")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    operation_id = str(uuid4())
    tool_call_id = "legacy-proof-call"
    tool_name = "save_application_jd_version"
    confirmation_token = "c" * 64
    raw_args = json.dumps(
        {"application_id": 7, "jd_text": "backend proof"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    normalized_args = json.loads(raw_args)
    with sessions() as session:
        conversation = Conversation(
            title="legacy verifier",
            scope_revision=0,
            pending_tool_call_id=tool_call_id,
            pending_operation_id=operation_id,
            pending_tool_name=tool_name,
            pending_args=raw_args,
            pending_human="confirm",
        )
        session.add(conversation)
        session.flush()
        repository.create_primary(
            session,
            operation_id=operation_id,
            conversation_id=conversation.id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            adapter_kind="legacy_deterministic",
            proposal_fingerprint=ledger_fingerprint(
                key,
                "write-operation-proposal-v1",
                normalized_args,
            ),
            confirmation_token_fingerprint=ledger_fingerprint(
                key,
                "write-operation-confirmation-token-v1",
                confirmation_token.encode("ascii"),
            ),
        )
        session.commit()
        conversation_id = conversation.id

    backend = _composition_module()._SqlAlchemyLegacyPendingIdentityBackend()
    lookup = LegacyConfirmationLookupIdentity(conversation_id=conversation_id)
    confirmation = LegacyApprovedConfirmationInput(
        decision="approved",
        operation_id=operation_id,
        confirmation_token=confirmation_token,
        edited_args_present=False,
        edited_args=None,
        rejection_feedback_present=False,
        rejection_feedback="",
    )
    with sessions() as read_session:
        with read_session.begin():
            snapshot = backend.read_snapshot(read_session, lookup, confirmation)
    assert snapshot["operation_id"] == operation_id
    assert snapshot["tool_call_id"] == tool_call_id
    assert snapshot["normalized_args"] == normalized_args
    assert snapshot["pending_confirmation_claim_id"] == ""

    with sessions() as write_session:
        write_session.execute(text("BEGIN IMMEDIATE"))
        locked = backend.locked_recheck(write_session, lookup, confirmation)
        claimed = backend.claim_cas(write_session, lookup, confirmation)
        assert locked["pending_confirmation_claim_id"] == ""
        assert claimed["pending_confirmation_claim_id"] == operation_id
        assert claimed["pending_confirmation_claimed_at"] is not None
        with pytest.raises(WriteOperationError, match="confirmation_claim_lost"):
            backend.claim_cas(write_session, lookup, confirmation)
        write_session.rollback()


def test_actual_adapter_policy_and_initial_registry_projection_equals_manifest() -> None:
    components = _production_components()
    expected = compile_tool_metadata_manifest(MODEL_TOOL_CATALOG.specs).to_dict()["legacy_boundary"]

    assert _actual_legacy_projection(components) == expected


def test_initial_route_handles_bind_through_the_completed_operation_port() -> None:
    components = _production_components()
    initial = components.initial_routes
    owner = initial.owner_lease_factory.open()
    issued: list[tuple[Any, OperationRouteIdentityV1]] = []
    try:
        for index, route_source in enumerate(ROUTE_SOURCES, start=1):
            issuer = initial.initial_issuer_for(LegacyRouteSourceV1(route_source))
            lease = issuer.open_request_lease(owner)
            token = issuer.issue(lease)
            route_handle = initial.initial_route_port.resolve_initial(token)
            identity = OperationRouteIdentityV1(
                operation_id=f"legacy-operation-{index}",
                tool_call_id=f"legacy-call-{index}",
                revision=index,
                arguments_digest="sha256:" + f"{index}" * 64,
            )
            operation_handle = components.operation_port.bind_legacy(route_handle, identity)
            entry = components.operation_port.require_legacy(operation_handle, identity)
            issued.append((operation_handle, identity))
            binding = initial.initial_route_port.require_route(route_handle)
            assert entry.operation_name == binding.name
            assert entry.adapter_kind == "legacy_deterministic"
    finally:
        owner.close()

    for operation_handle, identity in issued:
        with pytest.raises(ValueError, match="revoked|provenance"):
            components.operation_port.require_legacy(operation_handle, identity)


def test_proof_route_handles_bind_through_the_completed_operation_port(
    tmp_path: Path,
) -> None:
    proof_fixtures = importlib.import_module("tests.tool_metadata.test_legacy_confirmation_proof")
    components, backend = _proof_ready_production_components(tmp_path)
    confirmation = components.confirmation_routes
    prepare = getattr(proof_fixtures, "_prepare")
    issue = getattr(proof_fixtures, "_issue")
    close_issuance = getattr(proof_fixtures, "_close_issuance")
    operation_id = getattr(proof_fixtures, "OPERATION_ID")
    prepared, _read_session = prepare(confirmation, backend)
    proof, issuance_lease, write_session = issue(confirmation, backend, prepared)
    route_handle = confirmation.catalog.resolve_server_loaded(proof)
    identity = OperationRouteIdentityV1(
        operation_id=operation_id,
        tool_call_id="production-proof-tool-call",
        revision=1,
        arguments_digest="sha256:" + "9" * 64,
    )

    try:
        with pytest.raises(ValueError, match="Bundle|provenance|Registry"):
            _production_components().operation_port.bind_legacy(route_handle, identity)
        operation_handle = components.operation_port.bind_legacy(route_handle, identity)
        entry = components.operation_port.require_legacy(operation_handle, identity)
        assert entry.operation_name == "save_application_jd_version"
        assert entry.adapter_kind == "legacy_deterministic"
    finally:
        close_issuance(issuance_lease, write_session)

    with pytest.raises(ValueError, match="revoked|provenance"):
        components.operation_port.require_legacy(operation_handle, identity)


def test_composition_verifies_actual_ordered_adapters_before_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _composition_module()
    built_catalogs: list[Any] = []
    seal_calls: list[tuple[tuple[str, ...], str, str]] = []
    original_builder = legacy_specs.build_static_legacy_adapter_catalog

    def build_catalog() -> Any:
        catalog = original_builder()
        built_catalogs.append(catalog)
        return catalog

    def verify(names: tuple[str, ...], visibility: str, kind: str) -> None:
        seal_calls.append((names, visibility, kind))

    monkeypatch.setattr(
        composition,
        "build_static_legacy_adapter_catalog",
        build_catalog,
        raising=False,
    )
    monkeypatch.setattr(composition, "verify_legacy_boundary", verify, raising=False)

    components = _production_factory()(pending_identity_verifier_port=_pending_verifier())

    assert len(built_catalogs) == 1
    actual_names = tuple(adapter.name for adapter in built_catalogs[0].ordered_adapters)
    assert seal_calls == [(actual_names, "forbidden", "legacy_deterministic")]
    assert components.initial_routes.catalog is built_catalogs[0]


def test_legacy_seal_failure_publishes_no_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _composition_module()
    returned: list[object] = []

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ValueError("injected Legacy boundary failure")

    monkeypatch.setattr(composition, "verify_legacy_boundary", fail, raising=False)
    with pytest.raises(ValueError, match="Legacy boundary failure"):
        returned.append(_production_factory()(pending_identity_verifier_port=_pending_verifier()))
    assert returned == []


def _mutate_policy(catalog: Any, index: int) -> None:
    adapter = catalog.ordered_adapters[index]
    replacement = "forbidden" if adapter.chained_policy != "forbidden" else "same_adapter_only"
    object.__setattr__(adapter, "chained_policy", replacement)


def _mutate_route(catalog: Any, index: int) -> None:
    positions = tuple(
        (adapter, source_index)
        for adapter in catalog.ordered_adapters
        for source_index, _source in enumerate(adapter.initial_route_sources)
    )
    adapter, source_index = positions[index]
    sources = list(adapter.initial_route_sources)
    replacement = LegacyRouteSourceV1(
        "outcome_recording_action"
        if sources[source_index].value != "outcome_recording_action"
        else "jd_clarification"
    )
    sources[source_index] = replacement
    object.__setattr__(adapter, "initial_route_sources", tuple(sources))


def _mutate_ordinal(catalog: Any, index: int) -> None:
    adapter = catalog.ordered_adapters[index]
    object.__setattr__(adapter, "ordinal", adapter.ordinal + 10)


@pytest.mark.parametrize(
    ("mutation", "index"),
    [
        *[("policy", index) for index in range(3)],
        *[("route", index) for index in range(4)],
        *[("ordinal", index) for index in range(3)],
    ],
)
def test_actual_legacy_manifest_drift_fails_atomically(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    index: int,
) -> None:
    catalog = legacy_specs.build_static_legacy_adapter_catalog()
    mutators = {
        "policy": _mutate_policy,
        "route": _mutate_route,
        "ordinal": _mutate_ordinal,
    }
    mutators[mutation](catalog, index)
    composition = _composition_module()
    monkeypatch.setattr(
        composition,
        "build_static_legacy_adapter_catalog",
        lambda: catalog,
        raising=False,
    )
    returned: list[object] = []

    with pytest.raises((TypeError, ValueError), match="Legacy|Catalog|integrity|drift"):
        returned.append(_production_factory()(pending_identity_verifier_port=_pending_verifier()))
    assert returned == []


def test_late_component_failure_publishes_no_partial_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _composition_module()
    returned: list[object] = []

    def fail() -> None:
        raise RuntimeError("injected Compensation assembly failure")

    monkeypatch.setattr(
        composition,
        "prepare_compensation_handler_components",
        fail,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="Compensation assembly failure"):
        returned.append(_production_factory()(pending_identity_verifier_port=_pending_verifier()))
    assert returned == []


def test_production_component_factory_has_one_application_composition_caller() -> None:
    factory_name = "build_production_tool_metadata_components"
    callers: list[tuple[str, str]] = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                called = child.func
                if (
                    isinstance(called, ast.Name)
                    and called.id == factory_name
                    or isinstance(called, ast.Attribute)
                    and called.attr == factory_name
                ):
                    callers.append((path.relative_to(ROOT).as_posix(), node.name))

    assert callers == [("src/offerpilot/pilot_runtime/composition.py", "build_pilot_runtime")]
