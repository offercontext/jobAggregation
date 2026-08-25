from __future__ import annotations

import copy
import importlib
import inspect
import json
import pickle
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.metadata import (
    ToolMetadataBundleV1,
    canonical_json_bytes,
    freeze_json,
)
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.models import Application, ApplicationEvent, Base, InterviewNote
from tests.tool_metadata.golden import load_asset


def _required_module(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Task 6 module is missing: {name}")


def _required_api(module: ModuleType, name: str) -> Any:
    value = getattr(module, name, None)
    assert value is not None, f"Task 6 API is missing: {module.__name__}.{name}"
    return value


def _text(value: object) -> object:
    return getattr(value, "value", value)


class _LegacyIssuerProbe:
    def __init__(self, boundary: object) -> None:
        self.bundle_instance_token = boundary.bundle_instance_token
        self.registry_token = object()
        self.route_handle = object()
        self.binding = boundary.ordered_adapter_bindings[0]

    def require_route(self, route_handle: object) -> object:
        if route_handle is not self.route_handle:
            raise ValueError("Legacy route provenance mismatch")
        return self.binding


def _components_bundle_registry() -> tuple[object, ToolMetadataBundleV1, object]:
    module = _required_module("offerpilot.pilot_runtime.compensation")
    components = _required_api(module, "prepare_compensation_handler_components")()
    manifest = compile_tool_metadata_manifest(MODEL_TOOL_CATALOG.specs)
    projection = manifest.to_dict()
    bundle = ToolMetadataBundleV1(
        typed_catalog=MODEL_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=cast(dict[str, object], projection["legacy_boundary"]),
        compensation=components.metadata_projection(),
    )
    registry = components.bind(bundle.compensation_view())
    return components, bundle, registry


def _operation_port(bundle: ToolMetadataBundleV1, registry: object) -> object:
    metadata_module = _required_module("offerpilot.ai.tool_runtime.metadata")
    port_type = _required_api(metadata_module, "ToolOperationMetadataPort")
    return port_type(
        operation_view=bundle.operation_view(),
        legacy_boundary=bundle.legacy_boundary(),
        compensation_view=bundle.compensation_view(),
        compensation_registry=registry,
        legacy_route_issuer_port=_LegacyIssuerProbe(bundle.legacy_boundary()),
    )


def _committed_parent(
    primary_tool: str = "update_application_status",
    ordinal: int = 1,
) -> object:
    metadata_module = _required_module("offerpilot.ai.tool_runtime.metadata")
    parent_type = _required_api(
        metadata_module,
        "CommittedPrimaryOperationIdentityV1",
    )
    return parent_type(
        operation_id=f"parent-operation-{ordinal}",
        primary_tool=primary_tool,
        operation_role="primary",
        adapter_kind="typed",
        status="committed",
        terminal_payload_digest="sha256:" + str(ordinal) * 64,
    )


def _assert_transient(value: object) -> None:
    with pytest.raises(TypeError):
        copy.copy(value)
    with pytest.raises(TypeError):
        copy.deepcopy(value)
    with pytest.raises(TypeError):
        pickle.dumps(value)
    with pytest.raises(TypeError):
        asdict(value)
    to_json = getattr(value, "to_json", None)
    assert callable(to_json)
    with pytest.raises(TypeError):
        to_json()
    assert "0x" not in repr(value)


def _private_specs(value: object) -> tuple[object, ...]:
    return cast(tuple[object, ...], object.__getattribute__(value, "_ordered_specs"))


def _resolved_handler_specs(bundle: ToolMetadataBundleV1, registry: object) -> tuple[object, ...]:
    primary_tools = (
        "update_application_status",
        "create_application",
        "create_application_event",
        "add_note",
    )
    port = _operation_port(bundle, registry)
    return tuple(
        registry.resolve(
            port.bind_compensation(
                _committed_parent(primary_tool, ordinal),
                registry.bind_handler(binding),
            )
        )
        for ordinal, (primary_tool, binding) in enumerate(
            zip(primary_tools, bundle.compensation_view().ordered_handler_bindings),
            start=1,
        )
    )


def _compensation_handlers() -> dict[str, object]:
    _, bundle, registry = _components_bundle_registry()
    return {
        cast(str, spec.undo_payload_kind.value): spec
        for spec in _resolved_handler_specs(bundle, registry)
    }


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_compensation_registry_has_the_exact_four_sealed_handler_specs() -> None:
    components, bundle, registry = _components_bundle_registry()
    matrix = load_asset("tool_operation_matrix_0c10e05.json")
    resolved_specs = _resolved_handler_specs(bundle, registry)

    assert tuple(
        {
            "ordinal": spec.ordinal,
            "undo_payload_kind": _text(spec.undo_payload_kind),
            "compensation_kind": _text(spec.compensation_operation_kind),
            "adapter_kind": spec.adapter_kind,
            "result_contract": spec.result_contract,
            "undo_contract_version": spec.undo_contract_version,
            "handler_id": spec.handler_id,
        }
        for spec in resolved_specs
    ) == tuple(
        {
            "ordinal": item["ordinal"],
            "undo_payload_kind": item["undo_payload_kind"],
            "compensation_kind": item["compensation_kind"],
            "adapter_kind": item["adapter_kind"],
            "result_contract": item["result_contract"],
            "undo_contract_version": "write-undo-payload-v1",
            "handler_id": next(
                spec.handler_id
                for spec in resolved_specs
                if _text(spec.compensation_operation_kind) == item["compensation_kind"]
            ),
        }
        for item in matrix["compensation_operations"]
    )

    for spec in resolved_specs:
        assert inspect.isfunction(spec.validate_undo_payload)
        assert inspect.isfunction(spec.execute)
        assert spec.validate_undo_payload.__name__ != "<lambda>"
        assert spec.execute.__name__ != "<lambda>"
        assert "<locals>" not in spec.validate_undo_payload.__qualname__
        assert "<locals>" not in spec.execute.__qualname__


def test_compensation_registry_rejects_missing_duplicate_reordered_and_copied_specs() -> None:
    components, _, _ = _components_bundle_registry()
    module = _required_module("offerpilot.pilot_runtime.compensation")
    components_type = _required_api(module, "CompensationHandlerComponents")
    registry_type = _required_api(module, "CompensationHandlerRegistry")
    specs = _private_specs(components)

    invalid = (
        specs[:-1],
        (*specs, specs[0]),
        (specs[1], specs[0], *specs[2:]),
        (replace(specs[0]), *specs[1:]),
        (replace(specs[0], handler_id="wrong_handler_v1"), *specs[1:]),
        (replace(specs[0], execute=specs[1].execute), *specs[1:]),
    )
    for values in invalid:
        with pytest.raises((TypeError, ValueError)):
            components_type(values)
    with pytest.raises(TypeError):
        registry_type()


def test_static_components_only_become_a_final_registry_after_exact_bundle_binding() -> None:
    components, bundle, registry = _components_bundle_registry()

    assert not hasattr(components, "registry_token")
    assert not hasattr(components, "ordered_specs")
    assert not hasattr(registry, "ordered_specs")
    assert registry.bundle_instance_token is bundle.bundle_instance_token
    assert registry.compensation_view is bundle.compensation_view()
    with pytest.raises((TypeError, ValueError)):
        components.bind(bundle.compensation_view())

    other_components, other_bundle, _ = _components_bundle_registry()
    with pytest.raises((TypeError, ValueError)):
        other_components.bind(bundle.compensation_view())
    assert other_bundle.bundle_instance_token is not bundle.bundle_instance_token


def test_components_registry_and_handler_handles_fail_closed_after_identity_mutation() -> None:
    components = _required_api(
        _required_module("offerpilot.pilot_runtime.compensation"),
        "prepare_compensation_handler_components",
    )()
    object.__setattr__(components, "_ordered_specs", tuple(reversed(_private_specs(components))))
    with pytest.raises((TypeError, ValueError)):
        components.metadata_projection()

    _, bundle, registry = _components_bundle_registry()
    view = bundle.compensation_view()
    handler_handle = registry.bind_handler(view.ordered_handler_bindings[0])
    compensation_handle = _operation_port(bundle, registry).bind_compensation(
        _committed_parent(),
        handler_handle,
    )
    object.__setattr__(handler_handle, "_binding", view.ordered_handler_bindings[1])
    with pytest.raises((TypeError, ValueError)):
        registry.resolve(compensation_handle)

    _, bundle, registry = _components_bundle_registry()
    view = bundle.compensation_view()
    handler_handle = registry.bind_handler(view.ordered_handler_bindings[0])
    compensation_handle = _operation_port(bundle, registry).bind_compensation(
        _committed_parent(),
        handler_handle,
    )
    object.__setattr__(
        registry,
        "_binding_to_spec",
        ((view.ordered_handler_bindings[0], _private_specs(registry)[1]),),
    )
    with pytest.raises((TypeError, ValueError)):
        registry.resolve(compensation_handle)


def test_handler_handle_requires_the_exact_bundle_view_registry_and_binding_identity() -> None:
    _, bundle, registry = _components_bundle_registry()
    view = bundle.compensation_view()
    binding = view.ordered_handler_bindings[0]

    handle = registry.bind_handler(binding)
    _assert_transient(handle)
    with pytest.raises((TypeError, ValueError)):
        registry.resolve(handle)
    port = _operation_port(bundle, registry)
    compensation_handle = port.bind_compensation(_committed_parent(), handle)
    assert registry.resolve(compensation_handle) is _private_specs(registry)[0]

    with pytest.raises((TypeError, ValueError)):
        registry.bind_handler(replace(binding))
    with pytest.raises((TypeError, ValueError)):
        registry.resolve("undo:update_application_status")

    _, other_bundle, other_registry = _components_bundle_registry()
    other_view = other_bundle.compensation_view()
    with pytest.raises((TypeError, ValueError)):
        registry.bind_handler(other_view.ordered_handler_bindings[0])
    with pytest.raises((TypeError, ValueError)):
        other_registry.resolve(compensation_handle)

    module = _required_module("offerpilot.pilot_runtime.compensation")
    handle_type = _required_api(module, "CompensationHandlerHandle")
    with pytest.raises(TypeError):
        handle_type()


def test_handler_executes_in_the_exact_caller_owned_session_without_opening_another() -> None:
    _, bundle, registry = _components_bundle_registry()
    view = bundle.compensation_view()
    binding = view.ordered_handler_bindings[0]
    handler_handle = registry.bind_handler(binding)
    handler = registry.resolve(
        _operation_port(bundle, registry).bind_compensation(
            _committed_parent(),
            handler_handle,
        )
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    observed_sessions: list[Session] = []

    def remember_session(session: Session, transaction: object, connection: object) -> None:
        del transaction, connection
        observed_sessions.append(session)

    event.listen(Session, "after_begin", remember_session)
    try:
        with sessions() as session:
            application = Application(
                company_name="Example",
                position_name="Engineer",
                status="offer",
                closed_reason="accepted",
            )
            session.add(application)
            session.commit()
            application_id = application.id
            observed_sessions.clear()
            immutable_undo = freeze_json(
                {
                    "kind": "update_application_status",
                    "label": "撤销更新投递状态",
                    "application_id": application_id,
                    "before": {"status": "applied", "closed_reason": ""},
                    "expected_after": {
                        "status": "offer",
                        "closed_reason": "accepted",
                    },
                }
            )
            handler.validate_undo_payload(immutable_undo)
            message = handler.execute(session, immutable_undo)
            assert message == "已撤销最近一次 AI 写入：投递状态已恢复。"
            restored = session.get(Application, application_id)
            assert restored is not None
            assert (restored.status, restored.closed_reason) == ("applied", "")
            assert observed_sessions
            assert all(item is session for item in observed_sessions)
            with pytest.raises(ValueError, match="^undo_conflict$"):
                handler.execute(session, immutable_undo)
    finally:
        event.remove(Session, "after_begin", remember_session)
        engine.dispose()


def test_status_handler_exact_match_mismatch_and_caller_rollback() -> None:
    handler = _compensation_handlers()["update_application_status"]
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with sessions() as session:
            application = Application(
                company_name="Status Co",
                position_name="Engineer",
                status="offer",
                closed_reason="accepted",
            )
            session.add(application)
            session.commit()
            application_id = application.id
            undo = freeze_json(
                {
                    "kind": "update_application_status",
                    "label": "撤销更新投递状态",
                    "application_id": application_id,
                    "before": {"status": "applied", "closed_reason": ""},
                    "expected_after": {
                        "status": "offer",
                        "closed_reason": "accepted",
                    },
                }
            )

            assert handler.execute(session, undo) == "已撤销最近一次 AI 写入：投递状态已恢复。"
            restored = session.get(Application, application_id)
            assert restored is not None
            assert (restored.status, restored.closed_reason) == ("applied", "")
            session.rollback()

        with sessions() as session:
            preserved = session.get(Application, application_id)
            assert preserved is not None
            assert (preserved.status, preserved.closed_reason) == ("offer", "accepted")
            mismatch = freeze_json(
                {
                    "kind": "update_application_status",
                    "label": "撤销更新投递状态",
                    "application_id": application_id,
                    "before": {"status": "applied", "closed_reason": ""},
                    "expected_after": {
                        "status": "offer",
                        "closed_reason": "declined",
                    },
                }
            )
            with pytest.raises(ValueError, match="^undo_conflict$"):
                handler.execute(session, mismatch)
            session.rollback()

        with sessions() as session:
            preserved = session.get(Application, application_id)
            assert preserved is not None
            assert (preserved.status, preserved.closed_reason) == ("offer", "accepted")
    finally:
        engine.dispose()


def test_delete_application_handler_exact_match_mismatch_rollback_and_fk_guard() -> None:
    handler = _compensation_handlers()["delete_application"]
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    applied_at = datetime(2026, 8, 24, 9, 30, tzinfo=timezone.utc)
    updated_at = datetime(2026, 8, 25, 10, 45, tzinfo=timezone.utc)
    try:
        with sessions() as session:
            application = Application(
                company_name="Delete Co",
                position_name="Backend Engineer",
                job_url="https://example.test/jobs/1",
                status="applied",
                source="ai",
                notes="exact snapshot",
                applied_at=applied_at,
                closed_reason="",
                updated_at=updated_at,
            )
            session.add(application)
            session.commit()
            application_id = application.id
            expected_after = {
                "company_name": application.company_name,
                "position_name": application.position_name,
                "job_url": application.job_url,
                "status": application.status,
                "source": application.source,
                "notes": application.notes,
                "applied_at": _utc_text(application.applied_at),
                "closed_reason": application.closed_reason,
                "updated_at": _utc_text(application.updated_at),
            }
            undo = freeze_json(
                {
                    "kind": "delete_application",
                    "label": "撤销新建投递",
                    "application_id": application_id,
                    "expected_after": expected_after,
                }
            )

            assert handler.execute(session, undo) == "已撤销最近一次 AI 写入：新建投递已删除。"
            deleted = session.get(Application, application_id)
            assert deleted is not None
            session.refresh(deleted)
            assert deleted.deleted_at is not None
            session.rollback()

        with sessions() as session:
            preserved = session.get(Application, application_id)
            assert preserved is not None
            assert preserved.deleted_at is None
            mismatch = freeze_json(
                {
                    "kind": "delete_application",
                    "label": "撤销新建投递",
                    "application_id": application_id,
                    "expected_after": {**expected_after, "notes": "changed"},
                }
            )
            with pytest.raises(ValueError, match="^undo_conflict$"):
                handler.execute(session, mismatch)
            session.rollback()

        with sessions() as session:
            dependency = ApplicationEvent(
                application_id=application_id,
                event_type="interview",
                status="todo",
            )
            session.add(dependency)
            session.commit()
            dependency_id = dependency.id
            with pytest.raises(ValueError, match="^undo_conflict$"):
                handler.execute(session, undo)
            session.rollback()

        with sessions() as session:
            preserved = session.get(Application, application_id)
            assert preserved is not None
            assert preserved.deleted_at is None
            assert session.get(ApplicationEvent, dependency_id) is not None
    finally:
        engine.dispose()


def test_delete_event_handler_exact_datetime_tags_null_mismatch_and_rollback() -> None:
    handler = _compensation_handlers()["delete_application_event"]
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    scheduled_at = datetime(2026, 8, 27, 2, 15, tzinfo=timezone.utc)
    try:
        with sessions() as session:
            application = Application(company_name="Event Co", position_name="Engineer")
            session.add(application)
            session.flush()
            application_id = application.id
            application_event = ApplicationEvent(
                application_id=application_id,
                event_type="interview",
                subtype="technical",
                round=2,
                scheduled_at=scheduled_at,
                duration_minutes=75,
                location="Room 7",
                notes="bring portfolio",
                remind_at=None,
                status="todo",
            )
            application_event.tags = ["onsite", "backend"]
            session.add(application_event)
            session.commit()
            event_id = application_event.id
            expected_after = {
                "application_id": application_id,
                "event_type": application_event.event_type,
                "subtype": application_event.subtype,
                "tags": ["onsite", "backend"],
                "round": application_event.round,
                "scheduled_at": _utc_text(application_event.scheduled_at),
                "duration_minutes": application_event.duration_minutes,
                "location": application_event.location,
                "notes": application_event.notes,
                "remind_at": None,
                "status": application_event.status,
            }
            undo = freeze_json(
                {
                    "kind": "delete_application_event",
                    "label": "撤销新建日程",
                    "application_event_id": event_id,
                    "expected_after": expected_after,
                }
            )

            assert handler.execute(session, undo) == "已撤销最近一次 AI 写入：新建日程已删除。"
            assert session.get(ApplicationEvent, event_id) is None
            session.rollback()

        with sessions() as session:
            preserved = session.get(ApplicationEvent, event_id)
            assert preserved is not None
            assert preserved.tags == ["onsite", "backend"]
            assert preserved.remind_at is None
            mismatch = freeze_json(
                {
                    "kind": "delete_application_event",
                    "label": "撤销新建日程",
                    "application_event_id": event_id,
                    "expected_after": {**expected_after, "tags": ["backend", "onsite"]},
                }
            )
            with pytest.raises(ValueError, match="^undo_conflict$"):
                handler.execute(session, mismatch)
            session.rollback()

        with sessions() as session:
            assert session.get(ApplicationEvent, event_id) is not None
    finally:
        engine.dispose()


def test_delete_note_handler_exact_visible_and_null_parent_mismatch_and_rollback() -> None:
    handler = _compensation_handlers()["delete_note"]
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with sessions() as session:
            application = Application(company_name="Note Co", position_name="Engineer")
            session.add(application)
            session.flush()
            application_id = application.id
            note = InterviewNote(
                application_id=application_id,
                company="Note Co",
                position="Engineer",
                round="2",
                date="2026-08-25",
                questions="How do you test?",
                self_reflection="Use smaller steps",
                difficulty_points="Concurrency",
                mood="focused",
            )
            session.add(note)
            session.commit()
            note_id = note.id
            expected_after = {
                "application_id": application_id,
                "company": note.company,
                "position": note.position,
                "round": note.round,
                "date": note.date,
                "questions": note.questions,
                "self_reflection": note.self_reflection,
                "difficulty_points": note.difficulty_points,
                "mood": note.mood,
            }
            undo = freeze_json(
                {
                    "kind": "delete_note",
                    "label": "撤销保存复盘",
                    "note_id": note_id,
                    "expected_after": expected_after,
                }
            )

            assert handler.execute(session, undo) == "已撤销最近一次 AI 写入：复盘记录已删除。"
            assert session.get(InterviewNote, note_id) is None
            session.rollback()

        with sessions() as session:
            assert session.get(InterviewNote, note_id) is not None
            mismatch = freeze_json(
                {
                    "kind": "delete_note",
                    "label": "撤销保存复盘",
                    "note_id": note_id,
                    "expected_after": {**expected_after, "questions": "changed"},
                }
            )
            with pytest.raises(ValueError, match="^undo_conflict$"):
                handler.execute(session, mismatch)
            session.rollback()

        with sessions() as session:
            parent = session.get(Application, application_id)
            assert parent is not None
            parent.deleted_at = datetime.now(timezone.utc)
            session.commit()
            with pytest.raises(ValueError, match="^undo_conflict$"):
                handler.execute(session, undo)
            session.rollback()

            unbound = InterviewNote(
                application_id=None,
                company="Standalone Co",
                position="Engineer",
                round="",
                date="2026-08-25",
                questions="Q",
                self_reflection="R",
                difficulty_points="D",
                mood="calm",
            )
            session.add(unbound)
            session.commit()
            unbound_id = unbound.id
            unbound_undo = freeze_json(
                {
                    "kind": "delete_note",
                    "label": "撤销保存复盘",
                    "note_id": unbound_id,
                    "expected_after": {
                        "application_id": None,
                        "company": unbound.company,
                        "position": unbound.position,
                        "round": unbound.round,
                        "date": unbound.date,
                        "questions": unbound.questions,
                        "self_reflection": unbound.self_reflection,
                        "difficulty_points": unbound.difficulty_points,
                        "mood": unbound.mood,
                    },
                }
            )
            assert (
                handler.execute(session, unbound_undo) == "已撤销最近一次 AI 写入：复盘记录已删除。"
            )
            assert session.get(InterviewNote, unbound_id) is None
            session.rollback()

        with sessions() as session:
            assert session.get(InterviewNote, note_id) is not None
            assert session.get(InterviewNote, unbound_id) is not None
    finally:
        engine.dispose()


def test_handlers_require_recursively_immutable_exact_payloads_before_sql() -> None:
    _, bundle, registry = _components_bundle_registry()
    view = bundle.compensation_view()
    handler_handle = registry.bind_handler(view.ordered_handler_bindings[0])
    handler = registry.resolve(
        _operation_port(bundle, registry).bind_compensation(
            _committed_parent(),
            handler_handle,
        )
    )

    with pytest.raises((TypeError, ValueError)):
        handler.validate_undo_payload(
            {
                "kind": "update_application_status",
                "application_id": 1,
                "before": {"status": "applied", "closed_reason": ""},
                "expected_after": {"status": "offer", "closed_reason": ""},
            }
        )
    wrong = freeze_json(
        {
            "kind": "delete_application",
            "label": "撤销新建投递",
            "application_id": 1,
            "expected_after": {},
        }
    )
    with pytest.raises((TypeError, ValueError)):
        handler.validate_undo_payload(wrong)


def test_all_handlers_accept_the_canonical_ledger_round_trip_payload_shape() -> None:
    _, bundle, registry = _components_bundle_registry()
    payloads = (
        {
            "kind": "update_application_status",
            "label": "撤销更新投递状态",
            "application_id": 1,
            "before": {"status": "applied", "closed_reason": ""},
            "expected_after": {"status": "offer", "closed_reason": "accepted"},
        },
        {
            "kind": "delete_application",
            "label": "撤销新建投递",
            "application_id": 1,
            "expected_after": {
                "company_name": "Example",
                "position_name": "Engineer",
                "job_url": "",
                "status": "applied",
                "source": "ai",
                "notes": "",
                "applied_at": None,
                "closed_reason": "",
                "updated_at": "2026-08-25T00:00:00Z",
            },
        },
        {
            "kind": "delete_application_event",
            "label": "撤销新建日程",
            "application_event_id": 1,
            "expected_after": {
                "application_id": 1,
                "event_type": "interview",
                "subtype": "technical",
                "tags": ["onsite"],
                "round": 1,
                "scheduled_at": "2026-08-25T01:00:00Z",
                "duration_minutes": 60,
                "location": "Office",
                "notes": "",
                "remind_at": None,
                "status": "todo",
            },
        },
        {
            "kind": "delete_note",
            "label": "撤销保存复盘",
            "note_id": 1,
            "expected_after": {
                "application_id": None,
                "company": "Example",
                "position": "Engineer",
                "round": "1",
                "date": "2026-08-25",
                "questions": "Q",
                "self_reflection": "R",
                "difficulty_points": "D",
                "mood": "calm",
            },
        },
    )

    for handler, payload in zip(_resolved_handler_specs(bundle, registry), payloads):
        frozen = freeze_json(payload)
        persisted = json.loads(canonical_json_bytes(frozen))
        handler.validate_undo_payload(freeze_json(persisted))


def test_compensation_module_has_no_implicit_session_or_bare_name_dispatch() -> None:
    module = _required_module("offerpilot.pilot_runtime.compensation")
    source = Path(cast(str, module.__file__)).read_text(encoding="utf-8")

    assert "sessionmaker(" not in source
    assert "database_coordinator" not in source
    assert "SessionLocal" not in source
    assert "compensation_kind_for_undo" not in source
    assert "COMPENSATION_OPERATION_NAMES" not in source
