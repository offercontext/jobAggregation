from __future__ import annotations

import ast
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import pickle
from threading import Barrier, Event
from types import MappingProxyType, SimpleNamespace
from typing import Any, NoReturn

import pytest
from starlette.responses import JSONResponse

from offerpilot.ai.tool_runtime import legacy as legacy_runtime
from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_runtime.contracts import TransientToolRuntimeValue
from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.metadata import (
    CommittedPrimaryOperationIdentityV1,
    ToolMetadataBundleV1,
    canonical_json_bytes,
    freeze_json,
    materialize_json,
)
from offerpilot.ai.tool_runtime.protocol_seals import verify_legacy_boundary
from offerpilot.ai.tool_specs import legacy as legacy_specs
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.agent_runtime.events import canonical_json as journal_canonical_json
from offerpilot.pilot_runtime import contracts as runtime_contracts
from offerpilot.pilot_runtime.compensation import prepare_compensation_handler_components
from offerpilot.schemas import ChatMessageOut


ROOT = Path(__file__).parents[2]
PRODUCTION_ROOT = ROOT / "src" / "offerpilot"

ORDERED_ADAPTERS = (
    "save_application_jd_version",
    "create_application_submission_snapshot",
    "record_application_outcome",
)
DIRECT_ROUTES = (
    ("jd_clarification", "save_application_jd_version"),
    ("jd_deterministic_action", "save_application_jd_version"),
    ("submission_snapshot_action", "create_application_submission_snapshot"),
    ("outcome_recording_action", "record_application_outcome"),
)


class _NonLocalAbort(BaseException):
    pass


class _DuckSessionBoundAdapter:
    def __init__(self, session: object | None = None) -> None:
        self._session = session

    def bind(self, session: object) -> _DuckSessionBoundAdapter:
        return _DuckSessionBoundAdapter(session)


class _DuckLegacyExecutionContext:
    session = object()
    outcomes_repository = object()

    @property
    def jd_service(self) -> object:
        class FakeJDService:
            @staticmethod
            def create_version(*_args: object, **_kwargs: object) -> object:
                version = SimpleNamespace(
                    id=1,
                    application_id=1,
                    version_number=1,
                    source_kind="pilot",
                )
                return SimpleNamespace(version=version, replayed=False)

        return FakeJDService()


class _CapturedLegacyExecutor:
    def __init__(self) -> None:
        self.repository = object()

    def execute(self, _encoded_args: str, _context: object) -> str:
        return "{}"


class _ForbiddenSessionFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> NoReturn:
        self.calls += 1
        raise AssertionError("Legacy presentation opened an implicit Session")


def _component_graph() -> tuple[Any, ToolMetadataBundleV1, object]:
    catalog_factory = getattr(
        legacy_specs,
        "build_static_legacy_adapter_catalog",
        None,
    )
    assert callable(catalog_factory), "Task 7 must expose the exact static Adapter factory"
    factory = getattr(
        legacy_runtime,
        "build_unpublished_legacy_initial_route_components",
        None,
    )
    assert callable(factory), "Task 7 must expose the unpublished all-or-nothing factory"
    manifest = compile_tool_metadata_manifest(MODEL_TOOL_CATALOG.specs)
    projection = manifest.to_dict()
    compensation = prepare_compensation_handler_components()
    bundle = ToolMetadataBundleV1(
        typed_catalog=MODEL_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=projection["legacy_boundary"],  # type: ignore[arg-type]
        compensation=compensation.metadata_projection(),
    )
    runtime_container_token = object()
    components = factory(
        catalog=catalog_factory(),
        legacy_boundary=bundle.legacy_boundary(),
        runtime_container_token=runtime_container_token,
    )
    return components, bundle, runtime_container_token


def _components() -> Any:
    return _component_graph()[0]


def _source(value: str) -> Any:
    source_type = getattr(legacy_runtime, "LegacyRouteSourceV1", None)
    assert source_type is not None, "Task 7 must define the closed Legacy source enum"
    return source_type(value)


def _issuer(components: Any, source: str) -> Any:
    return components.initial_issuer_for(_source(source))


def _open_owner(components: Any) -> Any:
    factory = components.owner_lease_factory
    return factory.open()


def _issue_route(components: Any, source: str) -> tuple[Any, Any, Any, Any]:
    issuer = _issuer(components, source)
    owner = _open_owner(components)
    lease = issuer.open_request_lease(owner)
    token = issuer.issue(lease)
    handle = components.initial_route_port.resolve_initial(token)
    return owner, lease, token, handle


def _binding_name(components: Any, handle: Any) -> str:
    binding = components.initial_route_port.require_route(handle)
    assert type(binding).__name__ == "LegacyAdapterBindingV1"
    return binding.name


def test_static_catalog_has_exact_ordered_specs_and_approved_protocol_seal() -> None:
    components = _components()
    catalog = components.catalog
    adapters = catalog.ordered_adapters

    assert type(adapters) is tuple
    assert tuple(adapter.name for adapter in adapters) == ORDERED_ADAPTERS
    assert all(type(adapter).__name__ == "LegacyDeterministicAdapterSpec" for adapter in adapters)
    assert tuple(adapter.ordinal for adapter in adapters) == (1, 2, 3)
    assert tuple(adapter.chained_policy for adapter in adapters) == (
        "same_adapter_only",
        "forbidden",
        "forbidden",
    )
    assert tuple(adapter.initial_route_sources for adapter in adapters) == (
        (_source("jd_clarification"), _source("jd_deterministic_action")),
        (_source("submission_snapshot_action"),),
        (_source("outcome_recording_action"),),
    )
    assert tuple(
        tuple(field["field"] for field in adapter.editable_fields) for adapter in adapters
    ) == (
        ("jd_text", "source_url"),
        ("submitted_at", "note"),
        (
            "stage",
            "result",
            "feedback_text",
            "reflection_text",
            "next_action_text",
            "occurred_at",
        ),
    )
    assert (
        verify_legacy_boundary(
            tuple(adapter.name for adapter in adapters),
            "forbidden",
            "legacy_deterministic",
        )
        is None
    )

    for adapter in adapters:
        assert adapter.execute.__closure__ is None
        assert tuple(inspect.signature(adapter.execute).parameters) == (
            "encoded_args",
            "context",
        )
        closure = inspect.getclosurevars(adapter.execute)
        assert closure.nonlocals == {}
        assert not any(
            marker in type(value).__name__.lower()
            for value in closure.globals.values()
            for marker in ("repository", "service", "session")
        )

    mutated_catalog = legacy_specs.build_static_legacy_adapter_catalog()
    mutated = mutated_catalog.ordered_adapters[0]
    object.__setattr__(mutated, "name", "mutated_legacy_adapter")
    with pytest.raises((TypeError, ValueError), match="integrity|seal|mutation|drift"):
        mutated_catalog.require_integrity()


def test_static_adapter_and_catalog_identity_replacement_fail_closed() -> None:
    catalog = legacy_specs.build_static_legacy_adapter_catalog()
    replacement_order = tuple(list(catalog.ordered_adapters))
    assert replacement_order == catalog.ordered_adapters
    assert replacement_order is not catalog.ordered_adapters
    object.__setattr__(catalog, "_ordered_adapters", replacement_order)
    with pytest.raises((TypeError, ValueError), match="integrity|seal|mutation|drift"):
        catalog.require_integrity()

    catalog = legacy_specs.build_static_legacy_adapter_catalog()
    adapter = catalog.ordered_adapters[0]
    replacement_fields = tuple(
        MappingProxyType(dict(descriptor)) for descriptor in adapter.editable_fields
    )
    assert replacement_fields == adapter.editable_fields
    assert replacement_fields is not adapter.editable_fields
    object.__setattr__(adapter, "editable_fields", replacement_fields)
    with pytest.raises((TypeError, ValueError), match="integrity|seal|mutation|drift"):
        catalog.require_integrity()


def test_static_adapter_rejects_bound_method_that_captures_runtime_state() -> None:
    template = legacy_specs.build_static_legacy_adapter_catalog().ordered_adapters[0]
    captured = _CapturedLegacyExecutor()
    with pytest.raises(TypeError, match="module-level|named function|callable"):
        legacy_runtime.LegacyDeterministicAdapterSpec(
            ordinal=template.ordinal,
            name=template.name,
            editable_fields=template.editable_fields,
            chained_policy=template.chained_policy,
            initial_route_sources=template.initial_route_sources,
            describe=template.describe,
            validate=template.validate,
            presentation=template.presentation,
            execute=captured.execute,
        )


def test_static_catalog_declares_complete_legacy_presentation_bindings() -> None:
    adapters = legacy_specs.build_static_legacy_adapter_catalog().ordered_adapters
    for adapter in adapters:
        presentation = adapter.presentation
        assert type(presentation).__name__ == "LegacyPresentationBindingV1"
        assert presentation.implementation_id.startswith("legacy-presentation-")
        presentation.require_integrity()
        for callback in (
            presentation.confirmation_description,
            presentation.pending_details_projector,
            presentation.success_summary_projector,
        ):
            assert inspect.isfunction(callback)
            assert callback.__closure__ is None
            assert "<locals>" not in callback.__qualname__
        assert presentation.confirmation_description("{}") == adapter.describe("{}")


def test_legacy_presentation_uses_exact_read_context_and_matches_baseline(
    tmp_path: Path,
) -> None:
    from offerpilot.db import init_database
    from offerpilot.models import Application
    from offerpilot.repositories.application_jd_versions import ApplicationJDService
    from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository

    read_context_type = getattr(runtime_contracts, "LegacyReadContext", None)
    assert read_context_type is not None
    assert issubclass(read_context_type, TransientToolRuntimeValue)
    session_factory = init_database(tmp_path / "legacy-presentation.sqlite3")
    applications = ApplicationsRepository(session_factory)
    jd_service = ApplicationJDService(session_factory)
    application = applications.create(
        ApplicationCreate(company_name="Acme", position_name="Engineer")
    )
    version = jd_service.create_version(
        application.id,
        jd_text="baseline JD",
        source_url=None,
        source_kind="pilot",
        expected_current_version_id=None,
        idempotency_key="legacy-present-0001",
    ).version
    encoded_args = json.dumps(
        {
            "application_id": application.id,
            "jd_text": "updated JD",
            "source_url": None,
            "expected_current_version_id": version.id,
            "idempotency_key": "legacy-present-0002",
        }
    )
    adapters = legacy_specs.build_static_legacy_adapter_catalog().ordered_adapters
    with session_factory() as session:
        transaction = session.begin()
        context = read_context_type(session, applications, jd_service)
        assert not hasattr(context, "session")
        assert not hasattr(context, "applications")
        assert not hasattr(context, "jd_service")
        jd_presentation = adapters[0].presentation
        pending_application = Application(
            company_name="Must not autoflush",
            position_name="Pending",
        )
        session.add(pending_application)
        assert pending_application.id is None
        assert tuple(session.new) == (pending_application,)
        assert jd_presentation.confirmation_description(encoded_args) == adapters[0].describe(
            encoded_args
        )
        assert materialize_json(
            jd_presentation.pending_details_projector(encoded_args, context)
        ) == {
            "target": {
                "id": f"application-{application.id}",
                "kind": "application",
                "title": "Acme",
                "meta": "Engineer",
                "source": "pending_action",
            },
            "evidence": [
                {
                    "id": f"application-{application.id}",
                    "kind": "application",
                    "title": "Acme",
                    "meta": "Engineer",
                    "source": "pending_action",
                }
            ],
            "application_jd": {
                "current_version_number": 1,
                "proposed_version_number": 2,
            },
        }
        assert pending_application.id is None
        assert tuple(session.new) == (pending_application,)
        assert dict(adapters[1].presentation.pending_details_projector("{}", context)) == {}
        assert dict(adapters[2].presentation.pending_details_projector("{}", context)) == {}
        assert all(
            adapter.presentation.success_summary_projector("{}", context) == "岗位资料已保存。"
            for adapter in adapters
        )
        forbidden_factory = _ForbiddenSessionFactory()
        no_implicit_session_context = read_context_type(
            session,
            ApplicationsRepository(forbidden_factory),  # type: ignore[arg-type]
            ApplicationJDService(forbidden_factory),  # type: ignore[arg-type]
        )
        assert materialize_json(
            jd_presentation.pending_details_projector(
                encoded_args,
                no_implicit_session_context,
            )
        )["application_jd"] == {
            "current_version_number": 1,
            "proposed_version_number": 2,
        }
        assert forbidden_factory.calls == 0
        for invalid_application_id in (str(application.id), True):
            invalid_args = json.dumps(
                {
                    **json.loads(encoded_args),
                    "application_id": invalid_application_id,
                }
            )
            assert (
                materialize_json(
                    jd_presentation.pending_details_projector(
                        invalid_args,
                        no_implicit_session_context,
                    )
                )
                == {}
            )
        for invalid_encoded_args in ("{", "[]", "null", '"text"'):
            assert (
                materialize_json(
                    jd_presentation.pending_details_projector(
                        invalid_encoded_args,
                        no_implicit_session_context,
                    )
                )
                == {}
            )
        assert forbidden_factory.calls == 0
        assert repr(context) == "<LegacyReadContext>"
        with pytest.raises(TypeError, match="transient|serializ"):
            copy.copy(context)
        with pytest.raises(TypeError, match="transient|serializ"):
            copy.deepcopy(context)
        with pytest.raises(TypeError, match="transient|serializ"):
            pickle.dumps(context)
        with pytest.raises((TypeError, ValueError)):
            JSONResponse({"context": context})
        with pytest.raises((TypeError, ValueError)):
            runtime_contracts.PreparedStreamExecution(
                invocation_id="legacy-read-context",
                preparation_kind=runtime_contracts.PreparationKind.DETERMINISTIC_INITIAL,
                execution_mode=runtime_contracts.StreamExecutionMode.DIRECT,
                opaque_state=context,
            )
        transaction.rollback()
        with pytest.raises((TypeError, ValueError), match="context|transaction|integrity"):
            jd_presentation.pending_details_projector(encoded_args, context)

    with pytest.raises((TypeError, ValueError), match="context"):
        adapters[0].presentation.pending_details_projector(
            encoded_args,
            _DuckLegacyExecutionContext(),  # type: ignore[arg-type]
        )


def test_legacy_route_source_enum_is_closed_and_complete() -> None:
    source_type = getattr(legacy_runtime, "LegacyRouteSourceV1", None)
    assert source_type is not None
    assert tuple(item.value for item in source_type) == (
        "jd_clarification",
        "jd_deterministic_action",
        "submission_snapshot_action",
        "outcome_recording_action",
        "confirmation_resume",
    )
    with pytest.raises(ValueError):
        source_type("unknown")


def test_legacy_execution_context_is_session_bound_and_transient() -> None:
    context_type = getattr(runtime_contracts, "LegacyExecutionContext", None)
    assert context_type is not None
    parameters = inspect.signature(context_type).parameters
    assert tuple(parameters)[0] == "session"
    assert parameters["session"].default is inspect.Parameter.empty
    assert issubclass(context_type, TransientToolRuntimeValue)
    assert "Session" in str(parameters["session"].annotation)
    kwargs = {
        name: object()
        for name, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    with pytest.raises((TypeError, ValueError), match="Session|session|context"):
        context_type(**kwargs)
    source = inspect.getsource(context_type)
    assert "sessionmaker" not in source
    assert "create_engine" not in source


def test_legacy_execution_context_rejects_spoofed_session_and_adapter_types() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker

    from offerpilot.repositories.application_jd_versions import ApplicationJDService
    from offerpilot.repositories.application_outcomes import ApplicationOutcomesRepository

    class FakeTransaction:
        parent = None
        nested = False
        is_active = True

        def __init__(self, session: object) -> None:
            self.session = session

    class SpoofedSession:
        is_active = True

        def __init__(self) -> None:
            self.transaction = FakeTransaction(self)

        def in_transaction(self) -> bool:
            return True

        def get_transaction(self) -> FakeTransaction:
            return self.transaction

    SpoofedSession.__name__ = "Session"
    SpoofedSession.__module__ = "sqlalchemy.orm.evil"
    spoofed = SpoofedSession()
    with pytest.raises(TypeError, match="exact SQLAlchemy Session"):
        runtime_contracts.LegacyExecutionContext(
            spoofed,  # type: ignore[arg-type]
            _DuckSessionBoundAdapter(),  # type: ignore[arg-type]
            _DuckSessionBoundAdapter(),  # type: ignore[arg-type]
        )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    session_factory = sessionmaker(bind=engine)
    with Session(engine) as session, session.begin():
        with pytest.raises(TypeError, match="ApplicationJDService|jd_service"):
            runtime_contracts.LegacyExecutionContext(
                session,
                _DuckSessionBoundAdapter(),  # type: ignore[arg-type]
                ApplicationOutcomesRepository(session_factory),
            )
        with pytest.raises(
            TypeError,
            match="ApplicationOutcomesRepository|outcomes_repository",
        ):
            runtime_contracts.LegacyExecutionContext(
                session,
                ApplicationJDService(session_factory),
                _DuckSessionBoundAdapter(),  # type: ignore[arg-type]
            )


def test_static_legacy_executor_requires_exact_integrity_checked_context() -> None:
    adapter = legacy_specs.build_static_legacy_adapter_catalog().ordered_adapters[0]
    encoded_args = json.dumps(
        {
            "application_id": 1,
            "jd_text": "JD",
            "source_url": None,
            "expected_current_version_id": None,
            "idempotency_key": "legacy-context-0001",
        }
    )
    with pytest.raises((TypeError, ValueError), match="Legacy execution context|integrity"):
        adapter.execute(encoded_args, _DuckLegacyExecutionContext())  # type: ignore[arg-type]

    forged = object.__new__(runtime_contracts.LegacyExecutionContext)
    with pytest.raises((TypeError, ValueError), match="Legacy execution context|integrity"):
        adapter.execute(encoded_args, forged)


def test_legacy_execution_context_binds_exact_adapters_to_the_live_transaction(
    tmp_path: Path,
) -> None:
    from offerpilot.db import init_database
    from offerpilot.repositories.application_jd_versions import ApplicationJDService
    from offerpilot.repositories.application_outcomes import ApplicationOutcomesRepository

    session_factory = init_database(tmp_path / "legacy-context.sqlite3")
    with session_factory() as session:
        with session.begin():
            context = runtime_contracts.LegacyExecutionContext(
                session,
                ApplicationJDService(session_factory),
                ApplicationOutcomesRepository(session_factory),
            )
            assert context.session is session
            assert type(context.jd_service) is ApplicationJDService
            assert type(context.outcomes_repository) is ApplicationOutcomesRepository
            assert context.jd_service._session is session
            assert context.outcomes_repository._session is session
        with pytest.raises(ValueError, match="transaction|integrity"):
            _ = context.session


def test_component_factory_verifies_actual_catalog_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], str, str]] = []
    original = legacy_specs.verify_legacy_boundary

    def verify(names: Any, visibility: str, kind: str) -> None:
        ordered = tuple(names)
        calls.append((ordered, visibility, kind))
        original(ordered, visibility, kind)

    monkeypatch.setattr(legacy_specs, "verify_legacy_boundary", verify)
    components = _components()
    assert (
        tuple(adapter.name for adapter in components.catalog.ordered_adapters) == ORDERED_ADAPTERS
    )
    assert calls == [(ORDERED_ADAPTERS, "forbidden", "legacy_deterministic")]

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise ValueError("injected Legacy protocol seal failure")

    component_type = type(components)
    monkeypatch.setattr(legacy_specs, "verify_legacy_boundary", fail)
    with pytest.raises(ValueError, match="injected Legacy protocol seal failure"):
        _components()
    assert not any(type(value) is component_type for value in vars(legacy_specs).values())
    assert not any(type(value) is component_type for value in vars(legacy_runtime).values())


def test_four_source_bound_issuers_are_reusable_exact_and_not_detachable() -> None:
    components = _components()
    issuers = tuple(_issuer(components, source) for source, _name in DIRECT_ROUTES)

    assert len({id(issuer) for issuer in issuers}) == 4
    assert issuers[0] is not issuers[1]
    assert not hasattr(issuers[0], "owner_lease_factory")
    assert not hasattr(issuers[0], "request_lease_factory")
    assert not hasattr(issuers[0], "open_owner_lease")

    for (source, expected_name), issuer in zip(DIRECT_ROUTES, issuers):
        owner = _open_owner(components)
        lease = issuer.open_request_lease(owner)
        token = issuer.issue(lease)
        handle = components.initial_route_port.resolve_initial(token)
        assert _binding_name(components, handle) == expected_name
        owner.close()
        owner.close()

    with pytest.raises((KeyError, TypeError, ValueError), match="confirmation|initial|source"):
        components.initial_issuer_for(_source("confirmation_resume"))


def test_issuer_integrity_is_rechecked_during_resolve_and_route_use() -> None:
    components = _components()
    issuer = _issuer(components, "jd_clarification")
    owner = _open_owner(components)
    lease = issuer.open_request_lease(owner)
    token = issuer.issue(lease)
    port = components.initial_route_port
    object.__setattr__(issuer, "_source", _source("jd_deterministic_action"))
    with pytest.raises((TypeError, ValueError), match="integrity|issuer|source|drift"):
        port.resolve_initial(token)
    owner.close()

    components = _components()
    issuer = _issuer(components, "jd_clarification")
    owner = _open_owner(components)
    lease = issuer.open_request_lease(owner)
    token = issuer.issue(lease)
    port = components.initial_route_port
    handle = port.resolve_initial(token)
    object.__setattr__(issuer, "_source", _source("jd_deterministic_action"))
    with pytest.raises((TypeError, ValueError), match="integrity|issuer|source|drift"):
        port.require_route(handle)
    owner.close()


def test_equal_binding_and_component_container_replacement_fail_closed() -> None:
    components = _components()
    issuer = _issuer(components, "jd_clarification")
    owner = _open_owner(components)
    lease = issuer.open_request_lease(owner)
    binding = object.__getattribute__(issuer, "_binding")
    replacement_binding = type(binding)(
        ordinal=binding.ordinal,
        name=binding.name,
        provider_visibility=binding.provider_visibility,
        adapter_kind=binding.adapter_kind,
        chained_policy=binding.chained_policy,
    )
    assert replacement_binding == binding
    assert replacement_binding is not binding
    object.__setattr__(issuer, "_binding", replacement_binding)
    with pytest.raises((TypeError, ValueError), match="integrity|binding|issuer|drift"):
        issuer.issue(lease)
    owner.close()

    components = _components()
    issuers = object.__getattribute__(components, "_issuers")
    replacement_issuers = MappingProxyType(dict(issuers))
    assert replacement_issuers == issuers
    assert replacement_issuers is not issuers
    object.__setattr__(components, "_issuers", replacement_issuers)
    with pytest.raises((TypeError, ValueError), match="integrity|component|drift"):
        _ = components.initial_route_port


def test_port_and_route_bind_exact_bundle_boundary_and_runtime_container() -> None:
    components, bundle, runtime_container_token = _component_graph()
    boundary = bundle.legacy_boundary()
    assert components.initial_route_port.bundle_instance_token is boundary.bundle_instance_token
    assert components.runtime_container_token is runtime_container_token

    for source, expected_name in DIRECT_ROUTES:
        owner, _lease, _token, handle = _issue_route(components, source)
        binding = components.initial_route_port.require_route(handle)
        expected = next(
            item for item in boundary.ordered_adapter_bindings if item.name == expected_name
        )
        assert binding is expected
        owner.close()


def test_request_tokens_are_fresh_one_shot_and_aba_resistant() -> None:
    components = _components()
    issuer = _issuer(components, "jd_clarification")

    first_owner = _open_owner(components)
    first_lease = issuer.open_request_lease(first_owner)
    first_token = issuer.issue(first_lease)
    with pytest.raises((TypeError, ValueError), match="issued|once|lease|token"):
        issuer.issue(first_lease)
    first_handle = components.initial_route_port.resolve_initial(first_token)
    with pytest.raises((TypeError, ValueError), match="resolved|once|token"):
        components.initial_route_port.resolve_initial(first_token)
    first_owner.close()
    with pytest.raises((TypeError, ValueError), match="closed|revoked|lease|handle"):
        components.initial_route_port.require_route(first_handle)

    second_owner = _open_owner(components)
    second_lease = issuer.open_request_lease(second_owner)
    second_token = issuer.issue(second_lease)
    assert second_owner is not first_owner
    assert second_lease is not first_lease
    assert second_token is not first_token
    second_handle = components.initial_route_port.resolve_initial(second_token)
    assert _binding_name(components, second_handle) == "save_application_jd_version"
    second_owner.close()


def test_concurrent_single_token_resolve_has_exactly_one_winner() -> None:
    components = _components()
    issuer = _issuer(components, "jd_clarification")
    owner = _open_owner(components)
    lease = issuer.open_request_lease(owner)
    token = issuer.issue(lease)
    barrier = Barrier(16)

    def resolve() -> tuple[str, object]:
        barrier.wait()
        try:
            return "won", components.initial_route_port.resolve_initial(token)
        except (TypeError, ValueError) as exc:
            return "lost", exc

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = tuple(executor.map(lambda _index: resolve(), range(16)))
    winners = tuple(value for state, value in results if state == "won")
    assert len(winners) == 1
    assert sum(state == "lost" for state, _value in results) == 15
    assert _binding_name(components, winners[0]) == "save_application_jd_version"
    owner.close()


@pytest.mark.parametrize("winner", ("close", "resolve"))
def test_close_and_resolve_linearize_in_both_barrier_orderings(winner: str) -> None:
    components = _components()
    issuer = _issuer(components, "submission_snapshot_action")
    owner = _open_owner(components)
    lease = issuer.open_request_lease(owner)
    token = issuer.issue(lease)
    barrier = Barrier(2)
    first_done = Event()

    def close() -> object:
        barrier.wait()
        if winner == "resolve":
            assert first_done.wait(timeout=5)
        result = owner.close()
        if winner == "close":
            first_done.set()
        return result

    def resolve() -> object:
        barrier.wait()
        if winner == "close":
            assert first_done.wait(timeout=5)
        try:
            result: object = components.initial_route_port.resolve_initial(token)
        except (TypeError, ValueError) as exc:
            result = exc
        if winner == "resolve":
            first_done.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        close_future = executor.submit(close)
        resolve_future = executor.submit(resolve)
        assert close_future.result(timeout=10) is None
        resolved = resolve_future.result(timeout=10)

    if winner == "close":
        assert isinstance(resolved, (TypeError, ValueError))
    else:
        assert not isinstance(resolved, BaseException)
        with pytest.raises((TypeError, ValueError), match="closed|revoked|handle|lease"):
            components.initial_route_port.require_route(resolved)


def test_two_concurrent_requests_from_one_reusable_issuer_remain_independent() -> None:
    components = _components()
    issuer = _issuer(components, "outcome_recording_action")
    barrier = Barrier(2)

    def request() -> tuple[object, object, object]:
        with components.owner_lease_factory.open() as owner:
            lease = issuer.open_request_lease(owner)
            token = issuer.issue(lease)
            barrier.wait()
            handle = components.initial_route_port.resolve_initial(token)
            assert _binding_name(components, handle) == "record_application_outcome"
            return lease, token, handle

    with ThreadPoolExecutor(max_workers=2) as executor:
        left_future = executor.submit(request)
        right_future = executor.submit(request)
        left = left_future.result(timeout=10)
        right = right_future.result(timeout=10)
    assert all(a is not b for a, b in zip(left, right))
    for handle in (left[2], right[2]):
        with pytest.raises((TypeError, ValueError), match="closed|revoked|handle|lease"):
            components.initial_route_port.require_route(handle)


def test_owner_close_vs_child_open_and_issue_never_leaves_live_capability() -> None:
    components = _components()
    issuer = _issuer(components, "jd_deterministic_action")
    owner = _open_owner(components)
    barrier = Barrier(9)

    def open_and_issue() -> object:
        barrier.wait()
        try:
            lease = issuer.open_request_lease(owner)
            return issuer.issue(lease)
        except (TypeError, ValueError) as exc:
            return exc

    def close() -> None:
        barrier.wait()
        owner.close()

    with ThreadPoolExecutor(max_workers=9) as executor:
        futures = [executor.submit(open_and_issue) for _index in range(8)]
        close_future = executor.submit(close)
        results = tuple(future.result(timeout=10) for future in futures)
        assert close_future.result(timeout=10) is None

    for result in results:
        if isinstance(result, BaseException):
            continue
        with pytest.raises((TypeError, ValueError), match="closed|revoked|lease|token"):
            components.initial_route_port.resolve_initial(result)


def test_issue_vs_close_linearizes_without_a_live_token_escape() -> None:
    components = _components()
    issuer = _issuer(components, "jd_clarification")
    owner = _open_owner(components)
    lease = issuer.open_request_lease(owner)
    barrier = Barrier(2)

    def issue() -> object:
        barrier.wait()
        try:
            return issuer.issue(lease)
        except (TypeError, ValueError) as exc:
            return exc

    def close() -> None:
        barrier.wait()
        owner.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        issued_future = executor.submit(issue)
        close_future = executor.submit(close)
        issued = issued_future.result(timeout=10)
        assert close_future.result(timeout=10) is None
    if not isinstance(issued, BaseException):
        with pytest.raises((TypeError, ValueError), match="closed|revoked|lease|token"):
            components.initial_route_port.resolve_initial(issued)


def test_owner_close_cascades_to_all_children_tokens_and_handles() -> None:
    components = _components()
    owner = _open_owner(components)
    unresolved_issuer = _issuer(components, "jd_clarification")
    resolved_issuer = _issuer(components, "submission_snapshot_action")
    unresolved_lease = unresolved_issuer.open_request_lease(owner)
    resolved_lease = resolved_issuer.open_request_lease(owner)
    unresolved_token = unresolved_issuer.issue(unresolved_lease)
    resolved_token = resolved_issuer.issue(resolved_lease)
    resolved_handle = components.initial_route_port.resolve_initial(resolved_token)

    owner.close()
    assert owner.close() is None
    assert unresolved_lease.close() is None
    assert resolved_lease.close() is None
    with pytest.raises((TypeError, ValueError), match="closed|revoked|lease|token"):
        components.initial_route_port.resolve_initial(unresolved_token)
    with pytest.raises((TypeError, ValueError), match="closed|revoked|lease|handle"):
        components.initial_route_port.require_route(resolved_handle)
    with pytest.raises((TypeError, ValueError), match="closed|revoked|lease"):
        unresolved_issuer.issue(unresolved_lease)


def test_owner_close_clears_strong_references_after_metadata_integrity_drift() -> None:
    components = _components()
    owner, _lease, _token, _handle = _issue_route(
        components,
        "submission_snapshot_action",
    )
    registry = object.__getattribute__(owner, "_registry")
    object.__setattr__(
        registry,
        "_catalog",
        legacy_specs.build_static_legacy_adapter_catalog(),
    )
    assert owner.close() is None
    assert object.__getattribute__(registry, "_owners") == {}
    assert object.__getattribute__(registry, "_children") == {}
    assert object.__getattribute__(registry, "_tokens") == {}
    assert object.__getattribute__(registry, "_handles") == {}


def test_cross_source_container_and_port_are_rejected() -> None:
    left = _components()
    right = _components()
    left_owner = _open_owner(left)
    left_issuer = _issuer(left, "jd_clarification")
    sibling_issuer = _issuer(left, "jd_deterministic_action")
    foreign_issuer = _issuer(right, "jd_clarification")
    lease = left_issuer.open_request_lease(left_owner)

    with pytest.raises((TypeError, ValueError), match="issuer|source|lease"):
        sibling_issuer.issue(lease)
    with pytest.raises((TypeError, ValueError), match="container|owner|lease"):
        foreign_issuer.open_request_lease(left_owner)

    token = left_issuer.issue(lease)
    with pytest.raises((TypeError, ValueError), match="port|registry|container|token"):
        right.initial_route_port.resolve_initial(token)
    handle = left.initial_route_port.resolve_initial(token)
    with pytest.raises((TypeError, ValueError), match="port|registry|container|handle"):
        right.initial_route_port.require_route(handle)
    left_owner.close()
    with pytest.raises((TypeError, ValueError), match="closed|owner|lease"):
        left_issuer.open_request_lease(left_owner)


@pytest.mark.parametrize(
    "forged",
    (
        "save_application_jd_version",
        "jd_clarification",
        {"source": "jd_clarification"},
        {"tool_name": "save_application_jd_version"},
        object(),
    ),
)
def test_initial_port_rejects_strings_dicts_pending_fields_and_forged_tokens(
    forged: object,
) -> None:
    components = _components()
    with pytest.raises((TypeError, ValueError), match="token|initial|route|exact"):
        components.initial_route_port.resolve_initial(forged)

    with pytest.raises((TypeError, ValueError), match="token|initial|route|exact"):
        components.initial_route_port.resolve_initial(
            PendingAction(
                tool_call_id="legacy",
                tool_name="save_application_jd_version",
                args="{}",
                human="pending",
            )
        )


@pytest.mark.parametrize(
    ("source", "mode"),
    [
        (source, mode)
        for source, _name in DIRECT_ROUTES
        for mode in (
            "success",
            "pending_created",
            "exception",
            "cancelled",
            "base_exception",
        )
    ],
)
def test_every_source_closes_request_scope_on_all_exit_paths(source: str, mode: str) -> None:
    components = _components()
    issuer = _issuer(components, source)
    owner: Any | None = None
    token: Any | None = None
    handle: Any | None = None

    def run() -> str:
        nonlocal handle, owner, token
        with components.owner_lease_factory.open() as request_owner:
            owner = request_owner
            lease = issuer.open_request_lease(request_owner)
            token = issuer.issue(lease)
            handle = components.initial_route_port.resolve_initial(token)
            if mode == "exception":
                raise RuntimeError("ordinary failure")
            if mode == "cancelled":
                raise asyncio.CancelledError
            if mode == "base_exception":
                raise _NonLocalAbort
            return mode

    expected_error = {
        "exception": RuntimeError,
        "cancelled": asyncio.CancelledError,
        "base_exception": _NonLocalAbort,
    }.get(mode)
    if expected_error is None:
        assert run() == mode
    else:
        with pytest.raises(expected_error):
            run()
    assert owner is not None and token is not None and handle is not None
    with pytest.raises((TypeError, ValueError), match="closed|revoked|lease|handle"):
        components.initial_route_port.require_route(handle)

    # Closing one request never consumes the reusable application-scoped issuer.
    next_owner = _open_owner(components)
    next_lease = issuer.open_request_lease(next_owner)
    next_token = issuer.issue(next_lease)
    assert next_token is not token
    next_owner.close()


def test_transient_capabilities_and_routes_cannot_be_copied_or_serialized() -> None:
    components = _components()
    owner, lease, token, handle = _issue_route(components, "outcome_recording_action")
    issuer = _issuer(components, "outcome_recording_action")
    values = (
        components.owner_lease_factory,
        components.initial_route_port,
        issuer,
        owner,
        lease,
        token,
        handle,
    )

    for value in values:
        rendered = repr(value)
        assert rendered == f"<{type(value).__name__}>"
        assert "0x" not in rendered
        assert all(source not in rendered for source, _name in DIRECT_ROUTES)
        assert all(name not in rendered for name in ORDERED_ADAPTERS)
        with pytest.raises(TypeError, match="transient|serializ"):
            copy.copy(value)
        with pytest.raises(TypeError, match="transient|serializ"):
            copy.deepcopy(value)
        with pytest.raises(TypeError, match="transient|serializ"):
            pickle.dumps(value)
        with pytest.raises((TypeError, ValueError)):
            json.dumps(value)
        with pytest.raises((TypeError, ValueError)):
            freeze_json(value)
        with pytest.raises((TypeError, ValueError)):
            canonical_json_bytes(value)
        with pytest.raises((TypeError, ValueError)):
            runtime_contracts.freeze_json_mapping({"runtime": value})
        with pytest.raises(TypeError):
            asdict(value)

    owner.close()


def test_static_legacy_adapters_cannot_be_copied_or_generically_serialized() -> None:
    catalog = legacy_specs.build_static_legacy_adapter_catalog()
    for adapter in catalog.ordered_adapters:
        assert repr(adapter) == "<LegacyDeterministicAdapterSpec>"
        with pytest.raises(TypeError, match="transient|serializ"):
            copy.copy(adapter)
        with pytest.raises(TypeError, match="transient|serializ"):
            copy.deepcopy(adapter)
        with pytest.raises(TypeError, match="transient|serializ"):
            pickle.dumps(adapter)
        with pytest.raises(TypeError, match="transient|serializ"):
            asdict(adapter)
        with pytest.raises((TypeError, ValueError)):
            freeze_json(adapter)


def test_transient_route_values_are_rejected_by_every_persistence_and_transport_sink() -> None:
    components = _components()
    owner, _lease, _token, handle = _issue_route(components, "jd_clarification")

    # Pending persistence: this is otherwise a valid PendingAction and only the
    # encoded-args field is replaced with the transient route value.
    with pytest.raises((TypeError, ValueError)):
        PendingAction(
            tool_call_id="legacy-initial",
            tool_name="save_application_jd_version",
            args=handle,  # type: ignore[arg-type]
            human="confirm",
        )

    # Ledger identity: only the required primitive operation id is replaced.
    with pytest.raises((TypeError, ValueError)):
        CommittedPrimaryOperationIdentityV1(
            operation_id=handle,  # type: ignore[arg-type]
            primary_tool="update_application_status",
            operation_role="primary",
            adapter_kind="typed",
            status="committed",
            terminal_payload_digest="sha256:" + "0" * 64,
        )

    # Journal and checkpoint canonicalizers reject runtime capabilities rather
    # than calling repr or persisting an object address.
    with pytest.raises((TypeError, ValueError)):
        journal_canonical_json({"route": handle})
    with pytest.raises((TypeError, ValueError)):
        canonical_json_bytes(handle)

    # Pending/HTTP confirmation projection and SSE event are valid except for
    # the one transient JSON leaf.
    with pytest.raises((TypeError, ValueError)):
        runtime_contracts.PendingActionPayload(
            tool_name="save_application_jd_version",
            operation_id="operation-1",
            human="confirm",
            args=MappingProxyType({"route": handle}),  # type: ignore[dict-item]
            confirmation_token="token-1",
        )
    with pytest.raises((TypeError, ValueError)):
        runtime_contracts.ToolCallEvent(
            tool_call_id="legacy-initial",
            tool_name="save_application_jd_version",
            args_summary=handle,  # type: ignore[arg-type]
        )
    with pytest.raises((TypeError, ValueError)):
        runtime_contracts.MessageOutcome(handle)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        ChatMessageOut(
            id=1,
            conversation_id=1,
            role="assistant",
            content=handle,  # type: ignore[arg-type]
            created_at=datetime.now(timezone.utc),
        )
    with pytest.raises((TypeError, ValueError)):
        JSONResponse({"route": handle})
    with pytest.raises((TypeError, ValueError)):
        runtime_contracts.PreparedStreamExecution(
            invocation_id="legacy-initial",
            preparation_kind=runtime_contracts.PreparationKind.DETERMINISTIC_INITIAL,
            execution_mode=runtime_contracts.StreamExecutionMode.DIRECT,
            opaque_state=handle,
        )
    owner.close()


def test_initial_component_factory_has_only_final_composition_caller() -> None:
    factory_name = "build_unpublished_legacy_initial_route_components"
    consumers: list[tuple[str, str]] = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        if path == PRODUCTION_ROOT / "ai" / "tool_runtime" / "legacy.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(
                isinstance(child, ast.Call)
                and (
                    isinstance(child.func, ast.Name)
                    and child.func.id == factory_name
                    or isinstance(child.func, ast.Attribute)
                    and child.func.attr == factory_name
                )
                for child in ast.walk(node)
            ):
                consumers.append((path.relative_to(ROOT).as_posix(), node.name))
    assert consumers == [
        (
            "src/offerpilot/pilot_runtime/composition.py",
            "build_production_tool_metadata_components",
        )
    ]


def test_existing_legacy_factory_and_server_loaded_resolver_remain_unchanged() -> None:
    assert tuple(inspect.signature(legacy_specs.build_legacy_deterministic_catalog).parameters) == (
        "jd_service",
        "outcomes",
    )
    assert tuple(
        inspect.signature(
            legacy_runtime.LegacyDeterministicCatalog.resolve_server_loaded
        ).parameters
    ) == ("self", "pending")
    assert legacy_runtime.LEGACY_DETERMINISTIC_NAMES == frozenset(ORDERED_ADAPTERS)
    assert type(MappingProxyType({})) is MappingProxyType


class _CountingSessionFactory:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> Any:
        self.calls += 1
        return self.delegate(*args, **kwargs)


def _seed_legacy_executor_case(session_factory: Any) -> SimpleNamespace:
    from offerpilot.repositories.application_jd_versions import ApplicationJDService
    from offerpilot.repositories.application_outcomes import (
        ApplicationOutcomesRepository,
        SubmissionSnapshotCreate,
    )
    from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
    from offerpilot.repositories.resumes import ResumeCreate, ResumesRepository

    application = ApplicationsRepository(session_factory).create(
        ApplicationCreate(company_name="Legacy transaction", position_name="Engineer")
    )
    resume = ResumesRepository(session_factory).create(
        ResumeCreate(
            title="Legacy executor resume",
            content_json={"raw_text": "transaction-bound resume"},
        )
    )
    jd_version = (
        ApplicationJDService(session_factory)
        .create_version(
            application.id,
            jd_text="committed seed JD",
            source_url=None,
            source_kind="ui",
            expected_current_version_id=None,
            idempotency_key="legacy-seed-jd-0001",
        )
        .version
    )
    snapshot = (
        ApplicationOutcomesRepository(session_factory)
        .create_snapshot(
            SubmissionSnapshotCreate(
                application_id=application.id,
                resume_id=resume.id,
                jd_version_id=jd_version.id,
                material_kit_id=None,
                submitted_at=datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc),
                note="committed seed snapshot",
                source_kind="ui",
                idempotency_key="legacy-seed-snapshot-0001",
            )
        )
        .value
    )
    return SimpleNamespace(
        application_id=application.id,
        resume_id=resume.id,
        jd_version_id=jd_version.id,
        snapshot_id=snapshot.id,
    )


def _legacy_executor_args(
    adapter_name: str,
    seeded: SimpleNamespace,
    suffix: str,
) -> str:
    if adapter_name == "save_application_jd_version":
        payload = {
            "application_id": seeded.application_id,
            "jd_text": f"transactional JD {suffix}",
            "source_url": None,
            "expected_current_version_id": seeded.jd_version_id,
            "idempotency_key": f"legacy-jd-{suffix}-0001",
        }
    elif adapter_name == "create_application_submission_snapshot":
        payload = {
            "application_id": seeded.application_id,
            "resume_id": seeded.resume_id,
            "jd_version_id": seeded.jd_version_id,
            "material_kit_id": None,
            "submitted_at": "2026-08-25T09:30:00+00:00",
            "note": f"transactional snapshot {suffix}",
            "idempotency_key": f"legacy-snapshot-{suffix}-0001",
        }
    elif adapter_name == "record_application_outcome":
        payload = {
            "application_id": seeded.application_id,
            "submission_snapshot_id": seeded.snapshot_id,
            "application_event_id": None,
            "stage": "interview",
            "result": "advanced",
            "feedback_text": f"transactional feedback {suffix}",
            "reflection_text": "use a tighter example",
            "next_action_text": "prepare the next round",
            "feedback_tags": ["communication"],
            "occurred_at": "2026-08-25T10:00:00+00:00",
            "idempotency_key": f"legacy-outcome-{suffix}-0001",
        }
    else:  # pragma: no cover - the parametrized matrix is closed above
        raise AssertionError(f"unknown Legacy adapter: {adapter_name}")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _legacy_model_and_record_type(adapter_name: str) -> tuple[type[Any], str]:
    from offerpilot.models import (
        ApplicationJDVersion,
        ApplicationOutcome,
        ApplicationSubmissionSnapshot,
    )

    return {
        "save_application_jd_version": (
            ApplicationJDVersion,
            "application_jd_version",
        ),
        "create_application_submission_snapshot": (
            ApplicationSubmissionSnapshot,
            "application_submission_snapshot",
        ),
        "record_application_outcome": (ApplicationOutcome, "application_outcome"),
    }[adapter_name]


def _static_legacy_adapter(adapter_name: str) -> Any:
    catalog = legacy_specs.build_static_legacy_adapter_catalog()
    return next(adapter for adapter in catalog.ordered_adapters if adapter.name == adapter_name)


def _legacy_row_count(session: Any, model: type[Any]) -> int:
    from sqlalchemy import func, select

    count = session.scalar(select(func.count()).select_from(model))
    assert type(count) is int
    return count


def _legacy_execution_context(
    session: Any,
    session_factory: Any,
) -> runtime_contracts.LegacyExecutionContext:
    from offerpilot.repositories.application_jd_versions import ApplicationJDService
    from offerpilot.repositories.application_outcomes import ApplicationOutcomesRepository

    return runtime_contracts.LegacyExecutionContext(
        session,
        ApplicationJDService(session_factory),
        ApplicationOutcomesRepository(session_factory),
    )


@pytest.mark.parametrize("adapter_name", ORDERED_ADAPTERS)
def test_static_legacy_executors_share_the_caller_commit_and_rollback(
    tmp_path: Path,
    adapter_name: str,
) -> None:
    from offerpilot.db import init_database

    session_factory = init_database(tmp_path / f"{adapter_name}.sqlite3")
    seeded = _seed_legacy_executor_case(session_factory)
    adapter = _static_legacy_adapter(adapter_name)
    model, record_type = _legacy_model_and_record_type(adapter_name)
    with session_factory() as observer:
        baseline = _legacy_row_count(observer, model)

    with session_factory() as caller:
        transaction = caller.begin()
        context = _legacy_execution_context(caller, session_factory)
        encoded_result = adapter.execute(
            _legacy_executor_args(adapter_name, seeded, "rollback"),
            context,
        )
        assert json.loads(encoded_result)["record_type"] == record_type
        assert _legacy_row_count(caller, model) == baseline + 1
        with session_factory() as isolated_observer:
            assert _legacy_row_count(isolated_observer, model) == baseline
        transaction.rollback()
    with session_factory() as observer:
        assert _legacy_row_count(observer, model) == baseline

    with session_factory() as caller, caller.begin():
        context = _legacy_execution_context(caller, session_factory)
        encoded_result = adapter.execute(
            _legacy_executor_args(adapter_name, seeded, "commit"),
            context,
        )
        assert json.loads(encoded_result)["record_type"] == record_type
        assert _legacy_row_count(caller, model) == baseline + 1
    with session_factory() as observer:
        assert _legacy_row_count(observer, model) == baseline + 1


@pytest.mark.parametrize("adapter_name", ORDERED_ADAPTERS)
def test_static_legacy_executor_contexts_are_isolated_and_invalid_across_sessions(
    tmp_path: Path,
    adapter_name: str,
) -> None:
    from offerpilot.db import init_database

    session_factory = init_database(tmp_path / f"isolated-{adapter_name}.sqlite3")
    seeded = _seed_legacy_executor_case(session_factory)
    adapter = _static_legacy_adapter(adapter_name)
    model, _record_type = _legacy_model_and_record_type(adapter_name)
    with session_factory() as observer:
        baseline = _legacy_row_count(observer, model)

    with session_factory() as left, session_factory() as right:
        left_transaction = left.begin()
        right_transaction = right.begin()
        left_context = _legacy_execution_context(left, session_factory)
        right_context = _legacy_execution_context(right, session_factory)
        assert left_context.session is left
        assert right_context.session is right
        assert left_context.jd_service is not right_context.jd_service
        assert left_context.outcomes_repository is not right_context.outcomes_repository

        left_transaction.rollback()
        with pytest.raises((TypeError, ValueError), match="transaction|integrity"):
            adapter.execute(
                _legacy_executor_args(adapter_name, seeded, "stale-left"),
                left_context,
            )

        adapter.execute(
            _legacy_executor_args(adapter_name, seeded, "live-right"),
            right_context,
        )
        assert _legacy_row_count(right, model) == baseline + 1
        right_transaction.rollback()

    with session_factory() as observer:
        assert _legacy_row_count(observer, model) == baseline


@pytest.mark.parametrize(
    ("adapter_name", "repository_attribute", "write_method"),
    (
        ("save_application_jd_version", "jd_service", "create_version"),
        (
            "create_application_submission_snapshot",
            "outcomes_repository",
            "create_snapshot",
        ),
        ("record_application_outcome", "outcomes_repository", "create_outcome"),
    ),
)
def test_static_legacy_repository_exception_rolls_back_every_partial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_name: str,
    repository_attribute: str,
    write_method: str,
) -> None:
    from offerpilot.db import init_database

    session_factory = init_database(tmp_path / f"exception-{adapter_name}.sqlite3")
    seeded = _seed_legacy_executor_case(session_factory)
    adapter = _static_legacy_adapter(adapter_name)
    model, _record_type = _legacy_model_and_record_type(adapter_name)
    with session_factory() as observer:
        baseline = _legacy_row_count(observer, model)

    with pytest.raises(RuntimeError, match="after repository flush"):
        with session_factory() as caller, caller.begin():
            context = _legacy_execution_context(caller, session_factory)
            repository = getattr(context, repository_attribute)
            repository_type = type(repository)
            original = getattr(repository_type, write_method)

            def raise_after_write(
                self: object,
                *args: object,
                **kwargs: object,
            ) -> NoReturn:
                original(self, *args, **kwargs)
                raise RuntimeError("after repository flush")

            monkeypatch.setattr(repository_type, write_method, raise_after_write)
            adapter.execute(
                _legacy_executor_args(adapter_name, seeded, "exception"),
                context,
            )

    with session_factory() as observer:
        assert _legacy_row_count(observer, model) == baseline


@pytest.mark.parametrize(
    ("adapter_name", "bound_attribute"),
    (
        ("save_application_jd_version", "jd_service"),
        ("create_application_submission_snapshot", "outcomes_repository"),
        ("record_application_outcome", "outcomes_repository"),
    ),
)
def test_tampered_unbound_legacy_adapter_cannot_open_an_implicit_session(
    tmp_path: Path,
    adapter_name: str,
    bound_attribute: str,
) -> None:
    from offerpilot.db import init_database
    from offerpilot.repositories.application_jd_versions import ApplicationJDService
    from offerpilot.repositories.application_outcomes import ApplicationOutcomesRepository

    session_factory = init_database(tmp_path / f"implicit-{adapter_name}.sqlite3")
    seeded = _seed_legacy_executor_case(session_factory)
    adapter = _static_legacy_adapter(adapter_name)
    tracking_factory = _CountingSessionFactory(session_factory)
    model, _record_type = _legacy_model_and_record_type(adapter_name)
    with session_factory() as observer:
        baseline = _legacy_row_count(observer, model)

    with session_factory() as caller, caller.begin():
        context = runtime_contracts.LegacyExecutionContext(
            caller,
            ApplicationJDService(tracking_factory),  # type: ignore[arg-type]
            ApplicationOutcomesRepository(tracking_factory),  # type: ignore[arg-type]
        )
        bound_adapter = getattr(context, bound_attribute)
        bound_adapter._session = None
        with pytest.raises((TypeError, ValueError), match="integrity|Session|session"):
            adapter.execute(
                _legacy_executor_args(adapter_name, seeded, "implicit-session"),
                context,
            )
        assert tracking_factory.calls == 0

    with session_factory() as observer:
        assert _legacy_row_count(observer, model) == baseline


def test_legacy_execution_context_rejects_all_copy_and_transport_sinks(
    tmp_path: Path,
) -> None:
    from offerpilot.db import init_database

    session_factory = init_database(tmp_path / "legacy-context-sinks.sqlite3")
    with session_factory() as caller, caller.begin():
        context = _legacy_execution_context(caller, session_factory)
        assert repr(context) == "<LegacyExecutionContext>"
        with pytest.raises(TypeError, match="transient|serializ"):
            copy.copy(context)
        with pytest.raises(TypeError, match="transient|serializ"):
            copy.deepcopy(context)
        with pytest.raises(TypeError, match="transient|serializ"):
            pickle.dumps(context)
        with pytest.raises(TypeError):
            asdict(context)
        with pytest.raises((TypeError, ValueError)):
            json.dumps({"context": context})
        with pytest.raises((TypeError, ValueError)):
            freeze_json({"context": context})
        with pytest.raises((TypeError, ValueError)):
            canonical_json_bytes({"context": context})
        with pytest.raises((TypeError, ValueError)):
            runtime_contracts.PreparedStreamExecution(
                invocation_id="legacy-execution-context",
                preparation_kind=runtime_contracts.PreparationKind.DETERMINISTIC_INITIAL,
                execution_mode=runtime_contracts.StreamExecutionMode.DIRECT,
                opaque_state=context,
            )
        with pytest.raises((TypeError, ValueError)):
            JSONResponse({"context": context})
