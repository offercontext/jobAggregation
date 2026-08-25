from __future__ import annotations

import copy
import importlib
import pickle
from dataclasses import asdict, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker
from sqlalchemy.pool import StaticPool

from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    PreparedToolCall,
    ToolExecutionRecord,
    ToolSuccess,
)
from offerpilot.ai.tool_runtime.metadata import UndoBuilderBinding, freeze_json
from offerpilot.ai.tool_runtime.policy_types import CompensationKind, UndoPayloadKind, UndoPolicy
from tests.tool_metadata.factories import synthetic_tool_spec, write_metadata


_PROBE: list[str] = []
_SEED = freeze_json({"status": "applied"})
_TRANSACTION_TO_ROLLBACK: SessionTransaction | None = None
_NESTED_TRANSACTION_TO_ROLLBACK: SessionTransaction | None = None


def _capture_probe(context: object, args: object) -> object:
    del context, args
    _PROBE.append("capture")
    return _SEED


def _build_probe(seed: object, record: object) -> dict[str, object]:
    assert seed is _SEED
    assert type(record) is ToolExecutionRecord
    _PROBE.append("build")
    return {
        "kind": "delete_application",
        "label": "撤销新建投递",
        "application_id": 1,
        "expected_after": {"status": "applied"},
    }


def _capture_failure(context: object, args: object) -> object:
    del context, args
    _PROBE.append("capture_failure")
    raise RuntimeError("capture failed")


def _build_failure(seed: object, record: object) -> dict[str, object]:
    del seed, record
    _PROBE.append("build_failure")
    raise RuntimeError("build failed")


def _build_too_large(seed: object, record: object) -> dict[str, object]:
    del seed, record
    _PROBE.append("build_too_large")
    return {"kind": "delete_application", "payload": "x" * (64 * 1024)}


def _capture_rollback(context: object, args: object) -> object:
    del args
    transaction = getattr(context, "transaction")
    transaction.rollback()
    _PROBE.append("capture_rollback")
    return _SEED


def _build_rollback(seed: object, record: object) -> dict[str, object]:
    del seed, record
    assert _TRANSACTION_TO_ROLLBACK is not None
    _TRANSACTION_TO_ROLLBACK.rollback()
    _PROBE.append("build_rollback")
    return {"kind": "delete_application"}


def _build_nested_rollback(seed: object, record: object) -> dict[str, object]:
    del seed, record
    assert _NESTED_TRANSACTION_TO_ROLLBACK is not None
    _NESTED_TRANSACTION_TO_ROLLBACK.rollback()
    _PROBE.append("build_nested_rollback")
    return {"kind": "delete_application"}


def _build_keyboard_interrupt(seed: object, record: object) -> dict[str, object]:
    del seed, record
    _PROBE.append("build_keyboard_interrupt")
    raise KeyboardInterrupt


def _required_module() -> ModuleType:
    try:
        return importlib.import_module("offerpilot.pilot_runtime.primary_undo")
    except ModuleNotFoundError:
        pytest.fail("Task 6 module is missing: offerpilot.pilot_runtime.primary_undo")


def _required_api(module: ModuleType, name: str) -> Any:
    value = getattr(module, name, None)
    assert value is not None, f"Task 6 API is missing: {module.__name__}.{name}"
    return value


def _binding_and_record(
    *,
    capture: Any = _capture_probe,
    build: Any = _build_probe,
) -> tuple[UndoBuilderBinding, ToolExecutionRecord[dict[str, Any], dict[str, Any]]]:
    metadata = write_metadata(undo_policy=UndoPolicy.REQUIRED)
    operation = replace(
        metadata.operation,
        undo_payload_kind=UndoPayloadKind.UPDATE_APPLICATION_STATUS,
        compensation_kind=CompensationKind.UNDO_UPDATE_APPLICATION_STATUS,
        undo_builder_id="update_application_status_restore_v1",
        undo_seed_phase="before_execute",
    )
    metadata = replace(metadata, operation=operation)
    binding = UndoBuilderBinding(
        descriptor=operation,
        implementation_id=operation.undo_builder_id or "",
        capture_seed=capture,
        build_undo=build,
    )
    spec = replace(
        synthetic_tool_spec("synthetic_write", metadata),
        undo_builder_binding=binding,
    )
    prepared = PreparedToolCall(
        tool_call_id="call-1",
        spec=spec,
        arguments={"id": 1},
        typed_args={"id": 1},
        arguments_digest="sha256:" + "1" * 64,
        contract_fingerprint="sha256:" + "2" * 64,
        binding=BindingAudit("allowed", 1, ("application",)),
    )
    record = ToolExecutionRecord(
        prepared=prepared,
        outcome=ToolSuccess({"application_id": 1}),
        execution_started=True,
        operation_id="operation-1",
    )
    return binding, record


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
    assert "status" not in repr(value)


def _session() -> tuple[object, sessionmaker[Session], Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, sessions, sessions()


def test_primary_undo_checkpoint_enforces_capture_execute_build_commit_order() -> None:
    module = _required_module()
    capture_primary_undo = _required_api(module, "capture_primary_undo")
    build_primary_undo = _required_api(module, "build_primary_undo")
    binding, record = _binding_and_record()
    _PROBE.clear()

    engine, _, session = _session()
    try:
        transaction = session.begin()
        checkpoint = capture_primary_undo(
            binding,
            session,
            transaction,
            object(),
            {"id": 1},
        )
        _assert_transient(checkpoint)
        _PROBE.append("execute")
        undo = build_primary_undo(checkpoint, session, transaction, record)
        transaction.commit()
        _PROBE.append("commit")
    finally:
        session.close()
        engine.dispose()

    assert _PROBE == ["capture", "execute", "build", "commit"]
    assert undo["kind"] == "delete_application"
    assert undo["application_id"] == 1
    with pytest.raises((AttributeError, TypeError)):
        undo["kind"] = "mutated"
    with pytest.raises((AttributeError, TypeError)):
        undo["expected_after"]["status"] = "mutated"


def test_primary_undo_checkpoint_is_single_use_and_exact_binding_bound() -> None:
    module = _required_module()
    capture_primary_undo = _required_api(module, "capture_primary_undo")
    build_primary_undo = _required_api(module, "build_primary_undo")
    binding, record = _binding_and_record()
    engine, _, session = _session()
    try:
        transaction = session.begin()
        checkpoint = capture_primary_undo(binding, session, transaction, object(), {"id": 1})

        build_primary_undo(checkpoint, session, transaction, record)
        with pytest.raises((TypeError, ValueError)):
            build_primary_undo(checkpoint, session, transaction, record)

        other_binding, other_record = _binding_and_record()
        other_checkpoint = capture_primary_undo(
            other_binding, session, transaction, object(), {"id": 1}
        )
        with pytest.raises((TypeError, ValueError)):
            build_primary_undo(other_checkpoint, session, transaction, record)
        assert other_record.prepared.spec.undo_builder_binding is other_binding
        transaction.rollback()
    finally:
        session.close()
        engine.dispose()


def test_primary_undo_checkpoint_fails_closed_after_seed_identity_mutation() -> None:
    module = _required_module()
    capture_primary_undo = _required_api(module, "capture_primary_undo")
    build_primary_undo = _required_api(module, "build_primary_undo")
    binding, record = _binding_and_record()
    engine, _, session = _session()
    try:
        transaction = session.begin()
        checkpoint = capture_primary_undo(binding, session, transaction, object(), {"id": 1})
        object.__setattr__(
            checkpoint,
            "_PrimaryUndoCheckpoint__seed",
            freeze_json({"status": "tampered"}),
        )
        with pytest.raises((TypeError, ValueError), match="integrity|checkpoint"):
            build_primary_undo(checkpoint, session, transaction, record)
        with pytest.raises((TypeError, ValueError)):
            build_primary_undo(checkpoint, session, transaction, record)
        transaction.rollback()
    finally:
        session.close()
        engine.dispose()


def test_capture_and_build_failures_are_not_retried_or_turned_into_fallback_payloads() -> None:
    module = _required_module()
    capture_primary_undo = _required_api(module, "capture_primary_undo")
    build_primary_undo = _required_api(module, "build_primary_undo")
    capture_binding, _ = _binding_and_record(capture=_capture_failure)
    _PROBE.clear()
    engine, _, session = _session()
    try:
        transaction = session.begin()
        with pytest.raises(RuntimeError, match="capture failed"):
            capture_primary_undo(capture_binding, session, transaction, object(), {"id": 1})
        assert _PROBE == ["capture_failure"]

        build_binding, build_record = _binding_and_record(build=_build_failure)
        checkpoint = capture_primary_undo(build_binding, session, transaction, object(), {"id": 1})
        with pytest.raises(RuntimeError, match="build failed"):
            build_primary_undo(checkpoint, session, transaction, build_record)
        assert _PROBE.count("build_failure") == 1
        with pytest.raises((TypeError, ValueError)):
            build_primary_undo(checkpoint, session, transaction, build_record)
        assert _PROBE.count("build_failure") == 1
        transaction.rollback()
    finally:
        session.close()
        engine.dispose()


def test_primary_undo_rejects_payload_over_the_unchanged_64_kib_budget() -> None:
    module = _required_module()
    capture_primary_undo = _required_api(module, "capture_primary_undo")
    build_primary_undo = _required_api(module, "build_primary_undo")
    binding, record = _binding_and_record(build=_build_too_large)
    engine, _, session = _session()
    try:
        transaction = session.begin()
        checkpoint = capture_primary_undo(binding, session, transaction, object(), {"id": 1})

        with pytest.raises((TypeError, ValueError), match="64|65536|large|budget"):
            build_primary_undo(checkpoint, session, transaction, record)
        assert _PROBE.count("build_too_large") == 1
        transaction.rollback()
    finally:
        session.close()
        engine.dispose()


def test_primary_undo_revalidates_the_exact_active_transaction_around_both_callbacks() -> None:
    module = _required_module()
    capture_primary_undo = _required_api(module, "capture_primary_undo")
    build_primary_undo = _required_api(module, "build_primary_undo")
    binding, record = _binding_and_record()
    engine, sessions, session = _session()
    other_session = sessions()
    try:
        transaction = session.begin()
        other_transaction = other_session.begin()
        checkpoint = capture_primary_undo(binding, session, transaction, object(), {"id": 1})
        with pytest.raises((TypeError, ValueError)):
            build_primary_undo(checkpoint, other_session, other_transaction, record)
        transaction.rollback()
        with pytest.raises((TypeError, ValueError)):
            build_primary_undo(checkpoint, session, transaction, record)
        other_transaction.rollback()

        rollback_binding, _ = _binding_and_record(capture=_capture_rollback)
        transaction = session.begin()
        context = SimpleNamespace(transaction=transaction)
        with pytest.raises((TypeError, ValueError)):
            capture_primary_undo(
                rollback_binding,
                session,
                transaction,
                context,
                {"id": 1},
            )

        build_binding, build_record = _binding_and_record(build=_build_rollback)
        transaction = session.begin()
        checkpoint = capture_primary_undo(build_binding, session, transaction, object(), {"id": 1})
        global _TRANSACTION_TO_ROLLBACK
        _TRANSACTION_TO_ROLLBACK = transaction
        with pytest.raises((TypeError, ValueError)):
            build_primary_undo(checkpoint, session, transaction, build_record)
        _TRANSACTION_TO_ROLLBACK = None

        closed_session = sessions()
        closed_transaction = closed_session.begin()
        closed_checkpoint = capture_primary_undo(
            binding,
            closed_session,
            closed_transaction,
            object(),
            {"id": 1},
        )
        closed_session.close()
        with pytest.raises((TypeError, ValueError)):
            build_primary_undo(
                closed_checkpoint,
                closed_session,
                closed_transaction,
                record,
            )
    finally:
        _TRANSACTION_TO_ROLLBACK = None
        other_session.close()
        session.close()
        engine.dispose()


def test_primary_undo_revalidates_nested_savepoint_identity_and_propagates_base_exception() -> None:
    module = _required_module()
    capture_primary_undo = _required_api(module, "capture_primary_undo")
    build_primary_undo = _required_api(module, "build_primary_undo")
    engine, _, session = _session()
    global _NESTED_TRANSACTION_TO_ROLLBACK
    try:
        outer = session.begin()
        binding, record = _binding_and_record(build=_build_nested_rollback)
        checkpoint = capture_primary_undo(binding, session, outer, object(), {"id": 1})
        nested = session.begin_nested()
        _NESTED_TRANSACTION_TO_ROLLBACK = nested
        with pytest.raises((TypeError, ValueError)):
            build_primary_undo(checkpoint, session, outer, record)
        assert not nested.is_active
        outer.rollback()

        outer = session.begin()
        binding, record = _binding_and_record(build=_build_keyboard_interrupt)
        checkpoint = capture_primary_undo(binding, session, outer, object(), {"id": 1})
        nested = session.begin_nested()
        with pytest.raises(KeyboardInterrupt):
            build_primary_undo(checkpoint, session, outer, record)
        assert nested.is_active
        nested.rollback()
        outer.rollback()
        assert _PROBE.count("build_keyboard_interrupt") == 1
    finally:
        _NESTED_TRANSACTION_TO_ROLLBACK = None
        session.close()
        engine.dispose()


def test_primary_undo_module_is_shared_contract_code_without_tool_name_dispatch() -> None:
    module = _required_module()
    source = Path(cast(str, module.__file__)).read_text(encoding="utf-8")
    write_source = Path("src/offerpilot/ai/write_operations.py").read_text(encoding="utf-8")
    domain_sources = tuple(Path("src/offerpilot/ai/tool_specs").glob("*.py"))

    required_names = (
        "create_application",
        "update_application_status",
        "create_application_event",
        "add_note",
    )
    assert all(name not in source for name in required_names)
    assert "REQUIRED_UNDO_TOOL_NAMES" not in source
    assert ".capture_seed(" not in write_source
    assert ".build_undo(" not in write_source
    assert "capture_primary_undo(" in write_source
    assert "build_primary_undo(" in write_source
    assert all(
        "pilot_runtime.primary_undo" not in path.read_text(encoding="utf-8")
        for path in domain_sources
    )
