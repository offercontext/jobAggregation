from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
import importlib
import inspect
import json
from pathlib import Path
import pickle
import sqlite3
from threading import Barrier
from types import MappingProxyType
from typing import Any
from uuid import uuid4

import pytest

from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.contracts import TransientToolRuntimeValue
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1, freeze_json
from offerpilot.ai.tool_specs import legacy as legacy_specs
from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog
from offerpilot.ai.write_operations import ledger_fingerprint, load_or_create_ledger_key
from offerpilot.pilot_runtime.compensation import prepare_compensation_handler_components
from offerpilot.pilot_runtime.contracts import EditedArgs


_TEST_TOOL_CATALOG = build_model_tool_catalog()

ORDERED_ADAPTERS = (
    "save_application_jd_version",
    "create_application_submission_snapshot",
    "record_application_outcome",
)
OPERATION_ID = "a5ed5b94-62b5-45d9-a2d7-1eaa8fc2bdaa"
CLAIMED_AT = datetime(2026, 8, 25, 8, 9, 10, 123456, tzinfo=timezone.utc)
CONFIRMATION_TOKEN = "c" * 64
RAW_ARGS = json.dumps(
    {
        "application_id": 7,
        "jd_text": "original JD",
        "source_url": None,
        "expected_current_version_id": None,
        "idempotency_key": "legacy-proof-test-0001",
    },
    ensure_ascii=False,
    separators=(",", ":"),
)
CANONICAL_ARGS = json.dumps(
    json.loads(RAW_ARGS),
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
)


class _NonLocalAbort(BaseException):
    pass


class _DatetimeSubclass(datetime):
    pass


class _VerifierBackend:
    """Mutable database stand-in behind the exact verifier Port.

    The backend deliberately returns ordinary primitives.  The production Port
    must copy, verify and wrap them in its non-forgeable Locked Evidence before
    either Registry sees them.
    """

    def __init__(self, key: Any) -> None:
        self.key = key
        self.events: list[str] = []
        self.sessions: list[object] = []
        self.snapshot = self._snapshot(claimed=False)
        self.claim_mutations: dict[str, object] = {}
        self.claim_remove_keys: set[str] = set()
        self.fail_on: str | None = None
        self.session_factory: Any = None

    def _snapshot(self, *, claimed: bool) -> dict[str, object]:
        normalized = json.loads(RAW_ARGS)
        return {
            "adapter_kind": "legacy_deterministic",
            "operation_role": "primary",
            "route_source": "confirmation_resume",
            "conversation_id": 7,
            "conversation_scope_revision": 3,
            "pending_operation_id": OPERATION_ID,
            "operation_id": OPERATION_ID,
            "tool_call_id": "legacy-call-1",
            "tool_name": ORDERED_ADAPTERS[0],
            "fingerprint_key_id": self.key.key_id,
            "raw_args": RAW_ARGS,
            "normalized_args": normalized,
            "proposal_fingerprint": ledger_fingerprint(
                self.key,
                "write-operation-proposal-v1",
                normalized,
            ),
            "confirmation_token_fingerprint": ledger_fingerprint(
                self.key,
                "write-operation-confirmation-token-v1",
                CONFIRMATION_TOKEN.encode("ascii"),
            ),
            "authorization_scope_fingerprint": None,
            "input_fingerprint": None,
            "operation_request_fingerprint": None,
            "pending_confirmation_claim_id": OPERATION_ID if claimed else "",
            "pending_confirmation_claimed_at": CLAIMED_AT if claimed else None,
        }

    def read_snapshot(
        self,
        session: object,
        _lookup_identity: object,
        _confirmation_input: object,
    ) -> dict[str, object]:
        self.events.append("read_snapshot")
        self.sessions.append(session)
        if self.fail_on == "read_snapshot":
            raise ValueError("read snapshot failed")
        return copy.deepcopy(self.snapshot)

    def locked_recheck(
        self,
        session: object,
        _issuance_lease: object,
        _prepared_call: object,
    ) -> dict[str, object]:
        self.events.append("locked_mutable_recheck")
        self.sessions.append(session)
        if self.fail_on == "locked_mutable_recheck":
            raise ValueError("locked mutable recheck failed")
        return copy.deepcopy(self.snapshot)

    def claim_cas(
        self,
        session: object,
        _issuance_lease: object,
        _prepared_call: object,
    ) -> dict[str, object]:
        self.events.append("claim_cas")
        self.sessions.append(session)
        if self.fail_on == "claim_cas":
            raise ValueError("claim CAS failed")
        self.snapshot["pending_confirmation_claim_id"] = OPERATION_ID
        self.snapshot["pending_confirmation_claimed_at"] = CLAIMED_AT
        for key in self.claim_remove_keys:
            self.snapshot.pop(key, None)
        self.snapshot.update(self.claim_mutations)
        return copy.deepcopy(self.snapshot)


_EXECUTION_EVENTS: list[tuple[str, str, object]] = []
_EXECUTION_FAILURE: BaseException | None = None
_EXECUTION_OBSERVER: Any = None
_DESCRIPTION_EVENTS: list[str] = []
_DESCRIPTION_MODE = "edited"


def _spy_execute(encoded_args: str, context: object) -> str:
    _EXECUTION_EVENTS.append(("executor", encoded_args, context))
    if _EXECUTION_OBSERVER is not None:
        _EXECUTION_OBSERVER(encoded_args, context)
    if _EXECUTION_FAILURE is not None:
        raise _EXECUTION_FAILURE
    return '{"ok":true}'


def _stateful_describe(encoded_args: str) -> Any:
    _DESCRIPTION_EVENTS.append(encoded_args)
    if _DESCRIPTION_MODE == "invalid_after_prepare" and len(_DESCRIPTION_EVENTS) > 1:
        return object()
    return "confirm edited " + str(json.loads(encoded_args)["jd_text"])


def _proof_module() -> Any:
    try:
        return importlib.import_module("offerpilot.ai.tool_runtime.legacy_proof")
    except ModuleNotFoundError:
        pytest.fail("Task 8 must provide offerpilot.ai.tool_runtime.legacy_proof", pytrace=False)


def _route_module() -> Any:
    try:
        return importlib.import_module("offerpilot.pilot_runtime.legacy_route")
    except ModuleNotFoundError:
        pytest.fail("Task 8 must provide offerpilot.pilot_runtime.legacy_route", pytrace=False)


def _symbol(module: Any, name: str) -> Any:
    value = getattr(module, name, None)
    assert value is not None, f"Task 8 must expose {module.__name__}.{name}"
    return value


def _task11_symbol(module: Any, name: str) -> Any:
    value = getattr(module, name, None)
    assert value is not None, f"Task 11 must expose {module.__name__}.{name}"
    return value


def _catalog_with_spy_executor(*, describe: Any = None) -> Any:
    legacy_runtime = importlib.import_module("offerpilot.ai.tool_runtime.legacy")
    baseline = legacy_specs.build_static_adapter_catalog()
    first, *rest = baseline.ordered_adapters
    replaced = legacy_runtime.LegacyDeterministicAdapterSpec(
        ordinal=first.ordinal,
        name=first.name,
        editable_fields=first.editable_fields,
        chained_policy=first.chained_policy,
        initial_route_sources=first.initial_route_sources,
        describe=first.describe if describe is None else describe,
        validate=first.validate,
        presentation=first.presentation,
        execute=_spy_execute,
    )
    return legacy_runtime.LegacyStaticAdapterCatalogV1((replaced, *rest))


def _boundary() -> Any:
    manifest = compile_tool_metadata_manifest(_TEST_TOOL_CATALOG.specs)
    bundle = ToolMetadataBundleV1(
        typed_catalog=_TEST_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=manifest.to_dict()["legacy_boundary"],  # type: ignore[arg-type]
        compensation=prepare_compensation_handler_components().metadata_projection(),
    )
    return bundle.legacy_boundary()


def _components(
    tmp_path: Any,
    *,
    describe: Any = None,
) -> tuple[Any, _VerifierBackend, Any]:
    route_module = _route_module()
    from offerpilot.db import init_database

    data_dir = Path(tmp_path)
    data_dir.mkdir(parents=True, exist_ok=True)
    sessions = init_database(data_dir / f"legacy-proof-key-{uuid4()}.sqlite3")
    key = load_or_create_ledger_key(data_dir, sessions)
    backend = _VerifierBackend(key)
    backend.session_factory = sessions
    verifier_builder = _symbol(route_module, "build_legacy_pending_identity_verifier_port")
    verifier = verifier_builder(backend=backend, ledger_key=key)
    factory = _symbol(route_module, "build_unpublished_legacy_confirmation_components")
    components = factory(
        catalog=_catalog_with_spy_executor(describe=describe),
        legacy_boundary=_boundary(),
        runtime_container_token=object(),
        pending_identity_verifier_port=verifier,
    )
    return components, backend, key


def _approved_input(*, edited_args: EditedArgs | None = None) -> Any:
    proof_module = _proof_module()
    edits = edited_args if edited_args is not None else EditedArgs.missing()
    input_type = _symbol(proof_module, "LegacyApprovedConfirmationInput")
    return input_type(
        decision="approved",
        operation_id=OPERATION_ID,
        confirmation_token=CONFIRMATION_TOKEN,
        edited_args_present=not edits.is_missing(),
        edited_args=(edits.as_mapping if not edits.is_missing() else None),
        rejection_feedback_present=False,
        rejection_feedback="",
    )


def _lookup() -> Any:
    lookup_type = _symbol(_proof_module(), "LegacyConfirmationLookupIdentity")
    return lookup_type(conversation_id=7)


def _open_caller_session(backend: _VerifierBackend, label: str) -> Any:
    session = backend.session_factory()
    session.info["task8_label"] = label
    session.info["task8_transaction"] = session.begin()
    return session


def _open_unbegun_write_session(backend: _VerifierBackend, label: str) -> Any:
    session = backend.session_factory()
    session.info["task8_label"] = label
    assert not session.in_transaction()
    return session


def _dispose_caller_session(session: Any) -> None:
    transaction = session.get_transaction()
    if transaction is not None and transaction.is_active:
        transaction.rollback()
    session.info.pop("task8_transaction", None)
    session.close()


def _execution_context(backend: _VerifierBackend, session: Any) -> Any:
    from offerpilot.pilot_runtime.contracts import LegacyExecutionContext
    from offerpilot.repositories.application_jd_versions import ApplicationJDService
    from offerpilot.repositories.application_outcomes import ApplicationOutcomesRepository

    return LegacyExecutionContext(
        session,
        ApplicationJDService(backend.session_factory),
        ApplicationOutcomesRepository(backend.session_factory),
    )


def _retained_argument_material(components: Any) -> list[tuple[object, object]]:
    """White-box ownership probe; it does not add a production debug surface."""

    registry = components.preparation_registry
    return [
        (entry.raw_args, entry.effective_args)
        for entry in tuple(registry._entries.values())
        if entry.raw_args is not None or entry.effective_args is not None
    ]


def _assert_transient_privacy(value: object) -> None:
    rendered = repr(value)
    assert CONFIRMATION_TOKEN not in rendered
    assert OPERATION_ID not in rendered
    assert "original JD" not in rendered
    for operation in (copy.copy, copy.deepcopy, pickle.dumps, asdict):
        with pytest.raises((TypeError, ValueError)):
            operation(value)
    to_json = getattr(value, "to_json", None)
    assert callable(to_json)
    with pytest.raises((TypeError, ValueError)):
        to_json()
    with pytest.raises((TypeError, ValueError)):
        freeze_json({"transient": value})


def _close_issuance(lease: Any, session: Any, *, outcome: str | None = None) -> None:
    if outcome is None:
        lease.close()
    else:
        lease.close(outcome=outcome)
    _dispose_caller_session(session)


def _prepare(
    components: Any,
    backend: _VerifierBackend,
    *,
    confirmation_input: Any = None,
) -> tuple[Any, Any]:
    read_session = _open_caller_session(backend, "read")
    try:
        prepared = components.proof_issuer.prepare_server_loaded(
            read_session,
            _lookup(),
            _approved_input() if confirmation_input is None else confirmation_input,
        )
        assert not read_session.in_transaction()
        assert read_session.is_active
        assert backend.sessions[-1] is read_session
        return prepared, read_session
    finally:
        _dispose_caller_session(read_session)


def _issue(components: Any, backend: _VerifierBackend, prepared: Any) -> tuple[Any, Any, Any]:
    write_session = _open_unbegun_write_session(backend, "write")
    backend.snapshot = backend._snapshot(claimed=False)
    lease = components.pending_identity_verifier_port.open_issuance_lease(write_session, prepared)
    assert write_session.in_transaction()
    locked_evidence = components.pending_identity_verifier_port.locked_recheck(
        write_session,
        lease,
        prepared,
    )
    claim = components.pending_identity_verifier_port.bind_claim(
        write_session,
        lease,
        locked_evidence,
    )
    proof = components.proof_issuer.issue_after_claim(
        write_session,
        lease,
        claim,
        prepared,
    )
    return proof, lease, write_session


def _issue_route(components: Any, backend: _VerifierBackend) -> tuple[Any, Any, Any, Any]:
    prepared, _read_session = _prepare(components, backend)
    proof, lease, write_session = _issue(components, backend, prepared)
    handle = components.catalog.resolve_server_loaded(proof)
    return prepared, proof, handle, (lease, write_session)


def test_prepare_uses_one_caller_owned_read_transaction_without_execute_capability(
    tmp_path: Any,
) -> None:
    components, backend, _key = _components(tmp_path)

    prepared, read_session = _prepare(components, backend)

    assert backend.events == ["read_snapshot"]
    assert read_session.info["task8_label"] == "read"
    assert not read_session.in_transaction()
    assert type(prepared).__name__ == "PreparedLegacyCall"
    assert repr(prepared) == "<PreparedLegacyCall>"
    assert not hasattr(prepared, "execute")
    assert not hasattr(prepared, "adapter")
    assert not hasattr(prepared, "session")
    assert _EXECUTION_EVENTS == []


def test_prepare_reuses_the_single_legacy_argument_preparer_through_binding(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_runtime = importlib.import_module("offerpilot.ai.tool_runtime.legacy")
    route_module = _route_module()
    original = legacy_runtime.prepare_legacy_arguments
    calls: list[tuple[object, str, object, bool]] = []

    def capture(
        binding: object,
        encoded_args: str,
        edited_args: object,
        *,
        validate_unedited: bool = False,
    ) -> tuple[str, str]:
        calls.append((binding, encoded_args, edited_args, validate_unedited))
        return original(
            binding,
            encoded_args,
            edited_args,
            validate_unedited=validate_unedited,
        )

    monkeypatch.setattr(route_module, "prepare_legacy_arguments", capture, raising=False)
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    try:
        assert len(calls) == 1
        binding, encoded_args, edited_args, validate_unedited = calls[0]
        assert type(binding).__name__ == "LegacyPreparationBinding"
        assert encoded_args == CANONICAL_ARGS
        assert edited_args is None
        assert validate_unedited is True
    finally:
        prepared.close()


def test_prepared_input_port_projects_exact_edited_canonical_arguments_and_human(
    tmp_path: Any,
) -> None:
    global _DESCRIPTION_MODE

    _DESCRIPTION_EVENTS.clear()
    _DESCRIPTION_MODE = "edited"
    components, backend, _key = _components(tmp_path, describe=_stateful_describe)
    edits = EditedArgs.from_mapping(
        MappingProxyType(
            {
                "jd_text": "edited JD",
                "source_url": "https://example.com/job",
            }
        )
    )
    prepared, _read_session = _prepare(
        components,
        backend,
        confirmation_input=_approved_input(edited_args=edits),
    )
    expected_args = {
        "application_id": 7,
        "expected_current_version_id": None,
        "idempotency_key": "legacy-proof-test-0001",
        "jd_text": "edited JD",
        "source_url": "https://example.com/job",
    }
    expected_encoded = json.dumps(
        expected_args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    proof_module = _proof_module()
    input_type = _task11_symbol(proof_module, "PreparedLegacyInputV1")
    port_type = _task11_symbol(proof_module, "LegacyPreparedInputPort")
    port = components.prepared_input_port

    projected = port.require(
        prepared,
        operation_id=OPERATION_ID,
        tool_call_id="legacy-call-1",
        tool_name=ORDERED_ADAPTERS[0],
    )

    assert type(port) is port_type
    assert type(projected) is input_type
    assert isinstance(projected, TransientToolRuntimeValue)
    public_fields = {
        name
        for name, member in vars(input_type).items()
        if not name.startswith("_")
        and (isinstance(member, property) or type(member).__name__ == "member_descriptor")
    }
    assert public_fields == {"canonical_args", "encoded_args", "confirmation_human"}
    require_parameters = inspect.signature(port.require).parameters
    assert tuple(require_parameters) == (
        "prepared",
        "operation_id",
        "tool_call_id",
        "tool_name",
    )
    assert require_parameters["prepared"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for parameter in ("operation_id", "tool_call_id", "tool_name"):
        assert require_parameters[parameter].kind is inspect.Parameter.KEYWORD_ONLY
    port_surface = {
        name
        for name, member in vars(port_type).items()
        if not name.startswith("_") and (isinstance(member, property) or callable(member))
    }
    assert port_surface == {"require"}
    assert dict(projected.canonical_args) == expected_args
    assert projected.encoded_args == expected_encoded
    assert projected.confirmation_human == "confirm edited edited JD"
    assert _DESCRIPTION_EVENTS == [expected_encoded, expected_encoded]
    _assert_transient_privacy(port)
    _assert_transient_privacy(projected)
    for forbidden in (
        "execute",
        "adapter",
        "catalog",
        "registry",
        "preparation_registry",
        "proof_registry",
    ):
        assert not hasattr(projected, forbidden)
        assert not hasattr(port, forbidden)
    with pytest.raises(TypeError, match="factory|Port|port|created"):
        input_type(
            canonical_args=expected_args,
            encoded_args=expected_encoded,
            confirmation_human="confirm edited edited JD",
        )
    with pytest.raises(TypeError, match="factory|Port|port|created"):
        port_type(registry=components.preparation_registry)
    with pytest.raises((AttributeError, TypeError)):
        projected.encoded_args = RAW_ARGS
    with pytest.raises(TypeError):
        projected.canonical_args["jd_text"] = "forged"
    original_canonical = object.__getattribute__(projected, "_canonical_args")
    object.__setattr__(
        projected,
        "_canonical_args",
        freeze_json({**expected_args, "jd_text": "drifted JD"}),
    )
    with pytest.raises((TypeError, ValueError), match="canonical|drift|integrity|sealed"):
        _ = projected.canonical_args
    object.__setattr__(projected, "_canonical_args", original_canonical)
    drift_field = "_encoded_args" if hasattr(projected, "_encoded_args") else "encoded_args"
    object.__setattr__(projected, drift_field, RAW_ARGS)
    with pytest.raises((TypeError, ValueError), match="canonical|drift|integrity|sealed"):
        _ = projected.encoded_args
    prepared.close()


def test_prepared_input_port_rejects_foreign_wrong_identity_and_revoked_calls(
    tmp_path: Any,
) -> None:
    left, left_backend, _key = _components(tmp_path / "left-prepared-input")
    right, _right_backend, _other_key = _components(tmp_path / "right-prepared-input")
    foreign, _read_session = _prepare(left, left_backend)

    with pytest.raises((TypeError, ValueError), match="Registry|registry|foreign|identity"):
        right.prepared_input_port.require(
            foreign,
            operation_id=OPERATION_ID,
            tool_call_id="legacy-call-1",
            tool_name=ORDERED_ADAPTERS[0],
        )
    foreign.close()

    for identity in (
        {
            "operation_id": "ea8d7442-ebc8-4b5e-8b66-87914110eb39",
            "tool_call_id": "legacy-call-1",
            "tool_name": ORDERED_ADAPTERS[0],
        },
        {
            "operation_id": OPERATION_ID,
            "tool_call_id": "other-call",
            "tool_name": ORDERED_ADAPTERS[0],
        },
        {
            "operation_id": OPERATION_ID,
            "tool_call_id": "legacy-call-1",
            "tool_name": ORDERED_ADAPTERS[1],
        },
    ):
        wrong_identity, _read_session = _prepare(left, left_backend)
        try:
            with pytest.raises((TypeError, ValueError), match="operation|tool|identity|prepared"):
                left.prepared_input_port.require(wrong_identity, **identity)
        finally:
            wrong_identity.close()

    revoked, _read_session = _prepare(left, left_backend)
    revoked.close()
    with pytest.raises((TypeError, ValueError), match="revoked|closed|authority|prepared"):
        left.prepared_input_port.require(
            revoked,
            operation_id=OPERATION_ID,
            tool_call_id="legacy-call-1",
            tool_name=ORDERED_ADAPTERS[0],
        )


def test_invalid_recomputed_confirmation_human_revokes_before_claim_or_executor(
    tmp_path: Any,
) -> None:
    global _DESCRIPTION_MODE

    _EXECUTION_EVENTS.clear()
    _DESCRIPTION_EVENTS.clear()
    _DESCRIPTION_MODE = "invalid_after_prepare"
    components, backend, _key = _components(tmp_path, describe=_stateful_describe)
    prepared, _read_session = _prepare(components, backend)

    with pytest.raises((TypeError, ValueError), match="human|description|text|string"):
        components.prepared_input_port.require(
            prepared,
            operation_id=OPERATION_ID,
            tool_call_id="legacy-call-1",
            tool_name=ORDERED_ADAPTERS[0],
        )

    assert len(_DESCRIPTION_EVENTS) == 2
    assert backend.events == ["read_snapshot"]
    assert _EXECUTION_EVENTS == []
    write_session = _open_unbegun_write_session(backend, "invalid-description")
    try:
        with pytest.raises((TypeError, ValueError), match="revoked|closed|authority|prepared"):
            components.pending_identity_verifier_port.open_issuance_lease(
                write_session,
                prepared,
            )
    finally:
        _dispose_caller_session(write_session)


def _assert_prepared_attempt_is_cleared(
    components: Any,
    prepared: Any,
    entry: Any,
) -> None:
    registry = components.preparation_registry
    assert registry._prepared == {}
    assert registry._entries == {}
    assert entry.raw_args is None
    assert entry.effective_args is None
    assert entry.metadata is None
    assert entry.prepared_call is None
    assert entry.issuance_lease is None
    with pytest.raises((TypeError, ValueError), match="revoked|closed|authority|prepared"):
        registry._inspect_prepared(prepared)


def test_prepared_call_is_the_explicit_idempotent_attempt_owner_before_issuance(
    tmp_path: Any,
) -> None:
    """A successful prepare can be abandoned safely before a write lease exists."""

    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read = _prepare(components, backend)
    registry = components.preparation_registry
    entry = registry._prepared[prepared]
    assert entry.effective_args == CANONICAL_ARGS
    assert entry.metadata["confirmation_token"] == CONFIRMATION_TOKEN
    assert _retained_argument_material(components)

    assert prepared.close() is None
    assert prepared.close() is None

    _assert_prepared_attempt_is_cleared(components, prepared, entry)
    assert _retained_argument_material(components) == []
    assert _EXECUTION_EVENTS == []
    write_session = _open_unbegun_write_session(backend, "closed-preparation-write")
    try:
        with pytest.raises((TypeError, ValueError), match="revoked|closed|authority|prepared"):
            components.pending_identity_verifier_port.open_issuance_lease(
                write_session,
                prepared,
            )
    finally:
        _dispose_caller_session(write_session)


@pytest.mark.parametrize(
    "exit_kind",
    ("normal", "exception", "cancellation", "base_exception"),
)
def test_prepared_call_context_owner_revokes_on_every_preissuance_exit(
    tmp_path: Any,
    exit_kind: str,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read = _prepare(components, backend)
    entry = components.preparation_registry._prepared[prepared]

    if exit_kind == "normal":
        with prepared as owned:
            assert owned is prepared
    elif exit_kind == "exception":
        with pytest.raises(RuntimeError):
            with prepared:
                raise RuntimeError("preissuance failure")
    elif exit_kind == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            with prepared:
                raise asyncio.CancelledError()
    else:
        with pytest.raises(_NonLocalAbort):
            with prepared:
                raise _NonLocalAbort()

    _assert_prepared_attempt_is_cleared(components, prepared, entry)
    assert _retained_argument_material(components) == []
    assert _EXECUTION_EVENTS == []


def test_approved_input_deep_copies_edits_and_hides_secret_values() -> None:
    edits: dict[str, Any] = {"jd_text": "edited", "source_url": None}
    approved = _approved_input(edited_args=EditedArgs.from_mapping(MappingProxyType(edits)))
    edits["jd_text"] = "caller mutation"

    assert repr(approved) == "<LegacyApprovedConfirmationInput>"
    assert CONFIRMATION_TOKEN not in repr(approved)
    assert "edited" not in repr(approved)
    assert not hasattr(approved, "tool_name")
    with pytest.raises((AttributeError, TypeError)):
        approved.confirmation_token = "forged"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("operation_id", "edited_args_present", "edited_args"),
    (
        (None, False, None),
        (OPERATION_ID, False, None),
        (OPERATION_ID, True, MappingProxyType({})),
        (OPERATION_ID, True, MappingProxyType({"jd_text": "edited"})),
    ),
)
def test_approved_input_accepts_only_the_closed_approved_shape(
    operation_id: str | None,
    edited_args_present: bool,
    edited_args: object,
) -> None:
    input_type = _symbol(_proof_module(), "LegacyApprovedConfirmationInput")

    value = input_type(
        decision="approved",
        operation_id=operation_id,
        confirmation_token=CONFIRMATION_TOKEN,
        edited_args_present=edited_args_present,
        edited_args=edited_args,
        rejection_feedback_present=False,
        rejection_feedback="",
    )

    assert repr(value) == "<LegacyApprovedConfirmationInput>"
    assert CONFIRMATION_TOKEN not in repr(value)

    invalid_shapes = (
        {"decision": "rejected"},
        {"edited_args_present": True, "edited_args": None},
        {"edited_args_present": False, "edited_args": MappingProxyType({})},
        {"rejection_feedback_present": True},
        {"rejection_feedback": "must stay rejected-only"},
        {"operation_id": "not-a-uuid"},
        {"confirmation_token": ""},
    )
    baseline = {
        "decision": "approved",
        "operation_id": OPERATION_ID,
        "confirmation_token": CONFIRMATION_TOKEN,
        "edited_args_present": False,
        "edited_args": None,
        "rejection_feedback_present": False,
        "rejection_feedback": "",
    }
    for replacement in invalid_shapes:
        with pytest.raises((TypeError, ValueError)):
            input_type(**{**baseline, **replacement})


def test_lookup_identity_rejects_bool_int_integrity_drift() -> None:
    lookup_type = _symbol(_proof_module(), "LegacyConfirmationLookupIdentity")
    lookup = lookup_type(conversation_id=1)
    object.__setattr__(lookup, "_conversation_id", True)

    with pytest.raises((TypeError, ValueError), match="identity|integrity|conversation"):
        _ = lookup.conversation_id


def test_issue_requires_locked_mutable_recheck_then_claim_before_proof(
    tmp_path: Any,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _session = _prepare(components, backend)
    unclaimed_session = _open_caller_session(backend, "unclaimed-write")

    try:
        with pytest.raises((TypeError, ValueError), match="claim|lease|claimed"):
            components.proof_issuer.issue_after_claim(
                unclaimed_session,
                object(),
                object(),
                prepared,
            )
    finally:
        _dispose_caller_session(unclaimed_session)
    assert backend.events == ["read_snapshot"]

    proof, lease, write_session = _issue(components, backend, prepared)

    assert type(proof).__name__ == "LegacyRouteProof"
    assert backend.events == ["read_snapshot", "locked_mutable_recheck", "claim_cas"]
    assert backend.sessions[-1] is write_session
    assert components.catalog.resolve_server_loaded(proof) is not None
    _close_issuance(lease, write_session)


def test_open_issuance_starts_begin_immediate_and_rejects_preopened_transaction(
    tmp_path: Any,
) -> None:
    from sqlalchemy import event

    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, "begin-immediate")
    engine = backend.session_factory.kw["bind"]
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        lease = components.pending_identity_verifier_port.open_issuance_lease(
            write_session,
            prepared,
        )
        assert write_session.in_transaction()
        assert "BEGIN IMMEDIATE" in statements
        lease.close()
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)
        _dispose_caller_session(write_session)

    backend.snapshot = backend._snapshot(claimed=False)
    prepared, _read_session = _prepare(components, backend)
    ordinary_transaction_session = _open_caller_session(backend, "ordinary-transaction")
    try:
        with pytest.raises((TypeError, ValueError), match="BEGIN IMMEDIATE|transaction|unbegun"):
            components.pending_identity_verifier_port.open_issuance_lease(
                ordinary_transaction_session,
                prepared,
            )
    finally:
        prepared.close()
        _dispose_caller_session(ordinary_transaction_session)


def test_open_issuance_attach_baseexception_leaves_no_state_or_arguments(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, "attach-abort")
    lease_type = _symbol(_proof_module(), "LegacyRouteIssuanceLease")

    def abort_attach(*_args: object, **_kwargs: object) -> None:
        raise _NonLocalAbort()

    monkeypatch.setattr(lease_type, "_attach_cleanup", abort_attach)
    with pytest.raises(_NonLocalAbort):
        components.pending_identity_verifier_port.open_issuance_lease(
            write_session,
            prepared,
        )

    assert not write_session.in_transaction()
    assert components.pending_identity_verifier_port._issuance == {}
    assert components.pending_identity_verifier_port._evidence == {}
    assert components.preparation_registry._prepared == {}
    assert components.preparation_registry._entries == {}
    assert _retained_argument_material(components) == []
    _dispose_caller_session(write_session)


def test_claim_lease_rejects_claim_before_one_shot_locked_recheck(
    tmp_path: Any,
) -> None:
    """Raw claim primitives cannot stand in for the required pre-claim evidence."""

    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, "out-of-order-claim")
    lease = components.pending_identity_verifier_port.open_issuance_lease(
        write_session,
        prepared,
    )

    try:
        with pytest.raises((TypeError, ValueError), match="mutable recheck|evidence|order|exact"):
            components.pending_identity_verifier_port.bind_claim(
                write_session,
                lease,
                object(),
            )
        assert backend.events == ["read_snapshot"]
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


def test_locked_recheck_and_claim_ports_accept_only_exact_ordering_capabilities(
    tmp_path: Any,
) -> None:
    components, _backend, _key = _components(tmp_path)
    verifier = components.pending_identity_verifier_port

    assert tuple(inspect.signature(verifier.locked_recheck).parameters) == (
        "write_session",
        "issuance_lease",
        "prepared_call",
    )
    assert tuple(inspect.signature(verifier.bind_claim).parameters) == (
        "write_session",
        "issuance_lease",
        "locked_evidence",
    )


def test_locked_evidence_then_cas_claim_lease_then_proof_is_the_only_valid_order(
    tmp_path: Any,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, "ordered-claim")
    lease = components.pending_identity_verifier_port.open_issuance_lease(
        write_session,
        prepared,
    )

    try:
        locked_evidence = components.pending_identity_verifier_port.locked_recheck(
            write_session,
            lease,
            prepared,
        )
        claim = components.pending_identity_verifier_port.bind_claim(
            write_session,
            lease,
            locked_evidence,
        )
        proof = components.proof_issuer.issue_after_claim(
            write_session,
            lease,
            claim,
            prepared,
        )

        assert type(proof).__name__ == "LegacyRouteProof"
        assert backend.events == [
            "read_snapshot",
            "locked_mutable_recheck",
            "claim_cas",
        ]
    finally:
        _close_issuance(lease, write_session)


@pytest.mark.parametrize(
    ("mutation", "mutated"),
    (
        ("conversation_scope_revision", 4),
        ("operation_id", "8f225429-93ec-4d92-8aec-30ba4ec4e9e7"),
        ("tool_call_id", "stale-call"),
        ("tool_name", ORDERED_ADAPTERS[1]),
        ("authorization_scope_fingerprint", "hmac-sha256:" + "2" * 64),
        ("self_consistent_args", {"application_id": 8}),
    ),
)
def test_locked_recheck_rejects_post_prepare_persisted_identity_drift_before_claim(
    tmp_path: Any,
    mutation: str,
    mutated: object,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    if mutation == "self_consistent_args":
        mutated_args = {
            **json.loads(RAW_ARGS),
            **dict(mutated),  # type: ignore[arg-type]
        }
        backend.snapshot["raw_args"] = json.dumps(
            mutated_args,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        backend.snapshot["normalized_args"] = mutated_args
        backend.snapshot["proposal_fingerprint"] = ledger_fingerprint(
            backend.key,
            "write-operation-proposal-v1",
            mutated_args,
        )
    else:
        backend.snapshot[mutation] = mutated
    write_session = _open_unbegun_write_session(backend, "preclaim-drift")
    lease = components.pending_identity_verifier_port.open_issuance_lease(
        write_session,
        prepared,
    )

    try:
        with pytest.raises((TypeError, ValueError), match="identity|fingerprint|route|stale|null"):
            components.pending_identity_verifier_port.locked_recheck(
                write_session,
                lease,
                prepared,
            )
        assert backend.events == ["read_snapshot", "locked_mutable_recheck"]
        assert components.proof_registry._proofs == {}
        assert components.proof_registry._handles == {}
        assert components.pending_identity_verifier_port._issuance == {}
        assert _retained_argument_material(components) == []
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


@pytest.mark.parametrize("failure", ("snapshot", "invalid_json", "validation"))
def test_preparation_failure_never_claims_proves_routes_or_executes(
    tmp_path: Any,
    failure: str,
) -> None:
    components, backend, _key = _components(tmp_path)
    if failure == "snapshot":
        backend.fail_on = "read_snapshot"
        expected_error = "read snapshot failed"
    elif failure == "invalid_json":
        backend.snapshot["raw_args"] = "{"
        expected_error = "valid JSON"
    else:
        invalid = {**json.loads(RAW_ARGS), "application_id": True}
        backend.snapshot["raw_args"] = json.dumps(
            invalid,
            separators=(",", ":"),
        )
        backend.snapshot["normalized_args"] = invalid
        backend.snapshot["proposal_fingerprint"] = ledger_fingerprint(
            backend.key,
            "write-operation-proposal-v1",
            invalid,
        )
        expected_error = "application_id must be a positive integer"

    with pytest.raises((TypeError, ValueError), match=expected_error):
        read_session = _open_caller_session(backend, "failed-read")
        try:
            components.proof_issuer.prepare_server_loaded(
                read_session,
                _lookup(),
                _approved_input(),
            )
        finally:
            _dispose_caller_session(read_session)

    assert backend.events == ["read_snapshot"]
    assert _EXECUTION_EVENTS == []


@pytest.mark.parametrize(
    ("raw_value", "normalized_value"),
    (
        (1, True),
        (False, 0),
        (
            {"items": [{"enabled": True}, 1]},
            {"items": [{"enabled": 1}, True]},
        ),
    ),
    ids=("integer-vs-true", "false-vs-zero", "nested-type-drift"),
)
def test_prepare_rejects_json_type_drift_even_when_python_equality_matches(
    tmp_path: Any,
    raw_value: object,
    normalized_value: object,
) -> None:
    """Persisted JSON identity is canonical/type-sensitive, never Python ``==``."""

    components, backend, _key = _components(tmp_path)
    raw_payload = {
        **json.loads(RAW_ARGS),
        # The current Adapter deliberately ignores this extra field.  A failure
        # therefore proves the Pending raw/normalized identity check rejected
        # bool/int drift rather than an unrelated Adapter validation rule.
        "opaque_nested_value": raw_value,
    }
    normalized_payload = {
        **json.loads(RAW_ARGS),
        "opaque_nested_value": normalized_value,
    }
    assert raw_payload == normalized_payload
    assert json.dumps(
        raw_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) != json.dumps(
        normalized_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    backend.snapshot["raw_args"] = json.dumps(
        raw_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    backend.snapshot["normalized_args"] = normalized_payload
    backend.snapshot["proposal_fingerprint"] = ledger_fingerprint(
        backend.key,
        "write-operation-proposal-v1",
        raw_payload,
    )

    read_session = _open_caller_session(backend, "type-drift-read")
    try:
        with pytest.raises((TypeError, ValueError), match="normalized|identity|stale|type"):
            components.proof_issuer.prepare_server_loaded(
                read_session,
                _lookup(),
                _approved_input(),
            )
    finally:
        _dispose_caller_session(read_session)

    assert backend.events == ["read_snapshot"]
    assert _EXECUTION_EVENTS == []


def test_claim_or_proof_failure_revokes_the_attempt_and_prepared_arguments(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _read = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    backend.claim_mutations["operation_id"] = str(uuid4())
    write_session = _open_unbegun_write_session(backend, "write")
    lease = components.pending_identity_verifier_port.open_issuance_lease(write_session, prepared)
    evidence = components.pending_identity_verifier_port.locked_recheck(
        write_session,
        lease,
        prepared,
    )

    with pytest.raises((TypeError, ValueError), match="claim|operation|identity"):
        components.pending_identity_verifier_port.bind_claim(
            write_session,
            lease,
            evidence,
        )
    with pytest.raises((TypeError, ValueError), match="revoked|closed|attempt|claim"):
        components.pending_identity_verifier_port.bind_claim(
            write_session,
            lease,
            evidence,
        )
    assert _EXECUTION_EVENTS == []
    _close_issuance(lease, write_session)

    backend.snapshot = backend._snapshot(claimed=False)
    backend.claim_mutations.clear()
    prepared, _read = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, "write-2")
    lease = components.pending_identity_verifier_port.open_issuance_lease(write_session, prepared)
    evidence = components.pending_identity_verifier_port.locked_recheck(
        write_session,
        lease,
        prepared,
    )
    claim = components.pending_identity_verifier_port.bind_claim(
        write_session,
        lease,
        evidence,
    )

    registration_type = type(components.proof_issuer._registration_port)

    def fail_registration(*_args: object, **_kwargs: object) -> object:
        raise ValueError("proof registration failed")

    monkeypatch.setattr(registration_type, "issue", fail_registration)
    with pytest.raises((TypeError, ValueError), match="proof|registration"):
        components.proof_issuer.issue_after_claim(
            write_session,
            lease,
            claim,
            prepared,
        )
    with pytest.raises((TypeError, ValueError), match="revoked|consumed|attempt|prepared"):
        components.proof_issuer.issue_after_claim(
            write_session,
            lease,
            claim,
            prepared,
        )
    assert _EXECUTION_EVENTS == []
    _close_issuance(lease, write_session)


@pytest.mark.parametrize(
    ("field", "mutated"),
    (
        ("adapter_kind", "typed"),
        ("operation_role", "compensation"),
        ("route_source", "jd_clarification"),
        ("conversation_id", 8),
        ("conversation_scope_revision", 4),
        ("pending_operation_id", str(uuid4())),
        ("operation_id", str(uuid4())),
        ("tool_call_id", "other-call"),
        ("tool_name", ORDERED_ADAPTERS[1]),
        ("fingerprint_key_id", str(uuid4())),
        ("proposal_fingerprint", "hmac-sha256:" + "0" * 64),
        ("confirmation_token_fingerprint", "hmac-sha256:" + "1" * 64),
        ("authorization_scope_fingerprint", "hmac-sha256:" + "2" * 64),
        ("input_fingerprint", "hmac-sha256:" + "3" * 64),
        ("operation_request_fingerprint", "hmac-sha256:" + "4" * 64),
        ("pending_confirmation_claim_id", "other-claim"),
        ("raw_args", '{"application_id":999}'),
        ("normalized_args", {"application_id": 999}),
    ),
)
def test_locked_identity_fingerprint_key_and_claim_mismatch_fail_before_catalog(
    tmp_path: Any,
    field: str,
    mutated: object,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _read = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    backend.claim_mutations[field] = mutated
    write_session = _open_unbegun_write_session(backend, "write")
    lease = components.pending_identity_verifier_port.open_issuance_lease(write_session, prepared)
    evidence = components.pending_identity_verifier_port.locked_recheck(
        write_session,
        lease,
        prepared,
    )

    with pytest.raises((TypeError, ValueError), match="identity|fingerprint|claim|route|stale"):
        components.pending_identity_verifier_port.bind_claim(
            write_session,
            lease,
            evidence,
        )

    assert backend.events == ["read_snapshot", "locked_mutable_recheck", "claim_cas"]
    assert _EXECUTION_EVENTS == []
    _close_issuance(lease, write_session)


@pytest.mark.parametrize(
    ("mutation", "mutated"),
    (
        ("normalized_type_drift", True),
        ("claimed_at", None),
        ("claimed_at", "2026-08-25T08:09:10.123456Z"),
        ("claimed_at", True),
        ("claimed_at", 1),
        ("claimed_at", _DatetimeSubclass(2026, 8, 25, tzinfo=timezone.utc)),
        ("missing_key", "tool_call_id"),
        ("extra_key", "unexpected"),
    ),
)
def test_claim_cas_accepts_only_exact_shape_and_claim_only_delta(
    tmp_path: Any,
    mutation: str,
    mutated: object,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    write_session = _open_unbegun_write_session(backend, "claim-shape")
    lease = components.pending_identity_verifier_port.open_issuance_lease(
        write_session,
        prepared,
    )
    evidence = components.pending_identity_verifier_port.locked_recheck(
        write_session,
        lease,
        prepared,
    )
    if mutation == "normalized_type_drift":
        normalized = dict(backend.snapshot["normalized_args"])  # type: ignore[arg-type]
        normalized["application_id"] = mutated
        backend.claim_mutations["normalized_args"] = normalized
    elif mutation == "claimed_at":
        backend.claim_mutations["pending_confirmation_claimed_at"] = mutated
    elif mutation == "missing_key":
        backend.claim_remove_keys.add(str(mutated))
    else:
        backend.claim_mutations[str(mutated)] = "forbidden"

    try:
        with pytest.raises(
            (TypeError, ValueError),
            match="shape|claim|claimed|datetime|identity|normalized|JSON|type",
        ):
            components.pending_identity_verifier_port.bind_claim(
                write_session,
                lease,
                evidence,
            )
        assert backend.events == [
            "read_snapshot",
            "locked_mutable_recheck",
            "claim_cas",
        ]
        assert components.pending_identity_verifier_port._evidence == {}
        assert components.pending_identity_verifier_port._issuance == {}
        assert components.proof_registry._proofs == {}
        assert components.proof_registry._handles == {}
        assert _retained_argument_material(components) == []
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


def test_locked_evidence_is_one_shot_after_successful_claim_cas(tmp_path: Any) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    write_session = _open_unbegun_write_session(backend, "evidence-reuse")
    lease = components.pending_identity_verifier_port.open_issuance_lease(
        write_session,
        prepared,
    )
    evidence = components.pending_identity_verifier_port.locked_recheck(
        write_session,
        lease,
        prepared,
    )
    components.pending_identity_verifier_port.bind_claim(
        write_session,
        lease,
        evidence,
    )

    try:
        with pytest.raises((TypeError, ValueError), match="evidence|order|consumed|claim"):
            components.pending_identity_verifier_port.bind_claim(
                write_session,
                lease,
                evidence,
            )
        assert backend.events == [
            "read_snapshot",
            "locked_mutable_recheck",
            "claim_cas",
        ]
        assert components.pending_identity_verifier_port._evidence == {}
        assert components.pending_identity_verifier_port._issuance == {}
        assert components.proof_registry._proofs == {}
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


def test_locked_evidence_rejects_cross_verifier_session_and_lease_provenance(
    tmp_path: Any,
) -> None:
    _EXECUTION_EVENTS.clear()
    left, left_backend, _left_key = _components(tmp_path / "left")
    right, right_backend, _right_key = _components(tmp_path / "right")
    left_prepared, _left_read = _prepare(left, left_backend)
    right_prepared, _right_read = _prepare(right, right_backend)
    left_backend.snapshot = left_backend._snapshot(claimed=False)
    right_backend.snapshot = right_backend._snapshot(claimed=False)
    left_session = _open_unbegun_write_session(left_backend, "left-evidence")
    right_session = _open_unbegun_write_session(right_backend, "right-evidence")
    left_lease = left.pending_identity_verifier_port.open_issuance_lease(
        left_session,
        left_prepared,
    )
    right_lease = right.pending_identity_verifier_port.open_issuance_lease(
        right_session,
        right_prepared,
    )
    left_evidence = left.pending_identity_verifier_port.locked_recheck(
        left_session,
        left_lease,
        left_prepared,
    )
    right.pending_identity_verifier_port.locked_recheck(
        right_session,
        right_lease,
        right_prepared,
    )
    try:
        with pytest.raises((TypeError, ValueError), match="evidence|verifier|identity|order"):
            right.pending_identity_verifier_port.bind_claim(
                right_session,
                right_lease,
                left_evidence,
            )
        assert right_backend.events == ["read_snapshot", "locked_mutable_recheck"]
        assert right.pending_identity_verifier_port._issuance == {}
        assert right.proof_registry._proofs == {}
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(right_lease, right_session)
        _close_issuance(left_lease, left_session)

    components, backend, _key = _components(tmp_path / "same-verifier")
    prepared, _read = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    write_session = _open_unbegun_write_session(backend, "source-session")
    lease = components.pending_identity_verifier_port.open_issuance_lease(
        write_session,
        prepared,
    )
    evidence = components.pending_identity_verifier_port.locked_recheck(
        write_session,
        lease,
        prepared,
    )
    other_session = _open_caller_session(backend, "wrong-session")
    try:
        with pytest.raises((TypeError, ValueError), match="Session|transaction|provenance"):
            components.pending_identity_verifier_port.bind_claim(
                other_session,
                lease,
                evidence,
            )
        assert backend.events == ["read_snapshot", "locked_mutable_recheck"]
        assert components.pending_identity_verifier_port._issuance == {}
    finally:
        _dispose_caller_session(other_session)
        _close_issuance(lease, write_session)

    _session_components, target_backend, _target_key = _components(
        tmp_path / "same-verifier-target-session"
    )
    backend.snapshot = backend._snapshot(claimed=False)
    source_prepared, _read = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    target_prepared, _read = _prepare(components, backend)
    source_session = _open_unbegun_write_session(backend, "source-lease")
    target_session = _open_unbegun_write_session(target_backend, "target-lease")
    source_lease = components.pending_identity_verifier_port.open_issuance_lease(
        source_session,
        source_prepared,
    )
    target_lease = components.pending_identity_verifier_port.open_issuance_lease(
        target_session,
        target_prepared,
    )
    source_evidence = components.pending_identity_verifier_port.locked_recheck(
        source_session,
        source_lease,
        source_prepared,
    )
    backend.snapshot = backend._snapshot(claimed=False)
    components.pending_identity_verifier_port.locked_recheck(
        target_session,
        target_lease,
        target_prepared,
    )
    before = tuple(backend.events)
    try:
        with pytest.raises((TypeError, ValueError), match="evidence|order|lease|claim"):
            components.pending_identity_verifier_port.bind_claim(
                target_session,
                target_lease,
                source_evidence,
            )
        assert tuple(backend.events) == before
        assert set(components.pending_identity_verifier_port._issuance) == {source_lease}
        assert components.proof_registry._proofs == {}
    finally:
        _close_issuance(target_lease, target_session)
        _close_issuance(source_lease, source_session)


def test_cross_issuer_session_catalog_bundle_operation_scope_and_preparation_rejected(
    tmp_path: Any,
) -> None:
    left, left_backend, _key = _components(tmp_path / "left")
    right, right_backend, _other_key = _components(tmp_path / "right")
    prepared, _read = _prepare(left, left_backend)
    proof, lease, write_session = _issue(left, left_backend, prepared)
    other_session = _open_caller_session(left_backend, "other-session")

    try:
        for candidate in (
            lambda: right.catalog.resolve_server_loaded(proof),
            lambda: right.proof_consumer_port.consume(proof),
            lambda: right.proof_issuer.issue_after_claim(write_session, lease, object(), prepared),
            lambda: left.proof_issuer.issue_after_claim(
                other_session,
                lease,
                object(),
                prepared,
            ),
            lambda: left.proof_issuer.issue_after_claim(write_session, lease, object(), prepared),
            lambda: left.proof_issuer.issue_after_claim(write_session, lease, object(), object()),
        ):
            with pytest.raises(
                (TypeError, ValueError), match="issuer|Session|claim|Catalog|Bundle|prepar"
            ):
                candidate()
    finally:
        _dispose_caller_session(other_session)

    assert right_backend.events == []
    assert _EXECUTION_EVENTS == []
    _close_issuance(lease, write_session)


def test_proof_evidence_preparation_and_handle_are_noncopyable_nonserializable(
    tmp_path: Any,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, proof, handle, lease_and_session = _issue_route(components, backend)
    values = (
        prepared,
        proof,
        handle,
    )

    for value in values:
        assert "secret" not in repr(value).lower()
        for operation in (copy.copy, copy.deepcopy, pickle.dumps, asdict):
            with pytest.raises((TypeError, ValueError)):
                operation(value)
        with pytest.raises((TypeError, ValueError)):
            freeze_json({"proof": value})

    _close_issuance(*lease_and_session)


def test_real_locked_evidence_is_noncopyable_and_nonserializable(tmp_path: Any) -> None:
    components, backend, _key = _components(tmp_path)
    read_session = _open_caller_session(backend, "copy-evidence-read")
    evidence = components.pending_identity_verifier_port.read_snapshot(
        read_session,
        _lookup(),
        _approved_input(),
    )
    try:
        assert not read_session.in_transaction()
        assert repr(evidence) == "<LockedLegacyRouteEvidence>"
        for operation in (copy.copy, copy.deepcopy, pickle.dumps, asdict):
            with pytest.raises((TypeError, ValueError)):
                operation(evidence)
        with pytest.raises((TypeError, ValueError)):
            freeze_json({"evidence": evidence})
    finally:
        components.pending_identity_verifier_port._revoke_evidence(evidence)
        _dispose_caller_session(read_session)


def test_complete_legacy_proof_graph_is_transient_private_and_releases_references(
    tmp_path: Any,
) -> None:
    components, backend, _key = _components(tmp_path)
    approved = _approved_input()
    lookup = _lookup()
    adapter = components.proof_issuer._ordered_adapters[0]
    binding = components.preparation_registry._open_binding(adapter=adapter)
    initial_values = (
        approved,
        lookup,
        binding,
        components,
        components.preparation_registry,
        components.proof_registry,
        components.proof_issuer,
        components.proof_issuer._registration_port,
        components.proof_consumer_port,
        components.pending_identity_verifier_port,
        components.catalog,
    )
    try:
        for value in initial_values:
            _assert_transient_privacy(value)
    finally:
        components.preparation_registry._revoke_binding(binding)

    prepared, _read_session = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    write_session = _open_unbegun_write_session(backend, "privacy-write")
    lease = components.pending_identity_verifier_port.open_issuance_lease(
        write_session,
        prepared,
    )
    evidence = components.pending_identity_verifier_port.locked_recheck(
        write_session,
        lease,
        prepared,
    )
    for value in (prepared, lease, evidence):
        _assert_transient_privacy(value)
    claim = components.pending_identity_verifier_port.bind_claim(
        write_session,
        lease,
        evidence,
    )
    _assert_transient_privacy(claim)
    assert components.pending_identity_verifier_port._evidence == {}
    proof = components.proof_issuer.issue_after_claim(
        write_session,
        lease,
        claim,
        prepared,
    )
    handle = components.catalog.resolve_server_loaded(proof)
    for value in (proof, handle):
        _assert_transient_privacy(value)

    verifier_state = components.pending_identity_verifier_port._issuance[lease]
    assert verifier_state.locked_evidence is None
    assert verifier_state.locked_snapshot is None
    assert verifier_state.claimed_snapshot is None
    assert verifier_state.proof_snapshot_consumed is True
    proof_entry = components.proof_registry._proofs[proof]
    assert set(type(proof_entry).__slots__) == {
        "proof",
        "proof_identity",
        "issuer_token",
        "catalog_token",
        "bundle_token",
        "runtime_container_token",
        "issuance_lease",
        "claim_identity",
        "binding_identity",
        "prepared_identity",
        "adapter",
        "safe_metadata",
        "state",
        "handle",
        "execution_started",
        "integrity_seal",
    }
    assert frozenset(proof_entry.safe_metadata) == frozenset(
        {
            "adapter_kind",
            "operation_role",
            "route_source",
            "operation_id",
            "tool_call_id",
            "persisted_protocol_name",
            "fingerprint_key_id",
            "conversation_id",
            "conversation_scope_revision",
            "claim_id",
            "claimed_at",
            "pending_identity_digest",
            "effective_input_fingerprint",
            "operation_request_fingerprint",
            "adapter_ordinal",
        }
    )
    assert freeze_json(dict(proof_entry.safe_metadata)) == proof_entry.safe_metadata
    assert proof_entry.issuance_lease is lease
    assert not hasattr(proof_entry, "session")
    assert not hasattr(proof_entry, "transaction")
    assert not hasattr(proof_entry, "prepared_call")
    assert not hasattr(proof_entry, "raw_args")
    assert not hasattr(proof_entry, "effective_args")
    for forbidden_name in (
        "ledger_key",
        "secret",
        "confirmation_token",
        "evidence",
        "snapshot",
        "repository",
        "session",
        "transaction",
    ):
        assert not hasattr(proof_entry, forbidden_name)
    assert CONFIRMATION_TOKEN not in repr(proof_entry.safe_metadata)
    assert "original JD" not in repr(proof_entry.safe_metadata)
    assert backend.key.secret.hex() not in repr(proof_entry.safe_metadata)

    _close_issuance(lease, write_session)
    assert components.pending_identity_verifier_port._evidence == {}
    assert components.pending_identity_verifier_port._issuance == {}
    assert components.preparation_registry._prepared == {}
    assert components.preparation_registry._entries == {}
    assert components.proof_registry._proofs == {}
    assert components.proof_registry._handles == {}


def test_forged_exact_type_proof_and_locked_evidence_have_no_registry_authority(
    tmp_path: Any,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, proof, _handle, lease_and_session = _issue_route(components, backend)

    forged_proof = object.__new__(type(proof))
    with pytest.raises((TypeError, ValueError), match="proof|Registry|identity|forg"):
        components.catalog.resolve_server_loaded(forged_proof)

    evidence_type = _symbol(_route_module(), "LockedLegacyRouteEvidence")
    with pytest.raises((TypeError, ValueError), match="factory|verifier|private|evidence"):
        evidence_type()
    forged_evidence = object.__new__(evidence_type)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps, asdict):
        with pytest.raises((TypeError, ValueError)):
            operation(forged_evidence)
    assert "evidence" not in components.proof_issuer.issue_after_claim.__annotations__
    assert not hasattr(components.pending_identity_verifier_port, "last_evidence")
    assert _EXECUTION_EVENTS == []
    _close_issuance(*lease_and_session)


def test_proof_has_one_atomic_consumer_under_concurrency(tmp_path: Any) -> None:
    components, backend, _key = _components(tmp_path)
    _prepared, proof, _handle, lease_and_session = _issue_route(components, backend)
    # The first route above intentionally consumed its proof.  Issue a fresh one
    # so all racing workers start from the exact issued state.
    _close_issuance(*lease_and_session)
    backend.snapshot = backend._snapshot(claimed=False)
    prepared, _read = _prepare(components, backend)
    proof, lease, _write = _issue(components, backend, prepared)
    barrier = Barrier(8)

    def consume() -> object:
        barrier.wait()
        try:
            return components.catalog.resolve_server_loaded(proof)
        except (TypeError, ValueError) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _ordinal: consume(), range(8)))

    winners = [value for value in results if type(value).__name__ == "LegacyAdapterRouteHandle"]
    losers = [value for value in results if isinstance(value, (TypeError, ValueError))]
    assert len(winners) == 1
    assert len(losers) == 7
    with pytest.raises((TypeError, ValueError), match="once|resolved|consumed"):
        components.catalog.resolve_server_loaded(proof)
    assert _EXECUTION_EVENTS == []
    _close_issuance(lease, _write)


@pytest.mark.parametrize("transaction_exit", ("commit", "rollback"))
@pytest.mark.parametrize("protected_stage", ("catalog_resolve", "execute"))
def test_transaction_end_is_blocked_while_issuance_lease_is_live(
    tmp_path: Any,
    transaction_exit: str,
    protected_stage: str,
) -> None:
    """No transaction end may escape while the issuance capability is live."""

    from sqlalchemy.exc import DatabaseError as SQLAlchemyDatabaseError

    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    proof, lease, write_session = _issue(components, backend, prepared)
    handle = None
    context = None
    if protected_stage == "execute":
        handle = components.catalog.resolve_server_loaded(proof)
        context = _execution_context(backend, write_session)

    try:
        with pytest.raises(SQLAlchemyDatabaseError, match="authorized"):
            if transaction_exit == "commit":
                write_session.commit()
            else:
                write_session.rollback()

        with pytest.raises(
            (TypeError, ValueError),
            match="Session|transaction|closed|revoked|expired|provenance",
        ):
            if protected_stage == "catalog_resolve":
                components.catalog.resolve_server_loaded(proof)
            else:
                components.proof_consumer_port.execute(handle, context)
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


@pytest.mark.parametrize("transaction_end", ("commit", "rollback"))
def test_transaction_end_succeeds_after_issuance_lease_closes(
    tmp_path: Any,
    transaction_end: str,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, f"closed-{transaction_end}")
    lease = components.pending_identity_verifier_port.open_issuance_lease(
        write_session,
        prepared,
    )

    try:
        lease.close()
        getattr(write_session, transaction_end)()
        assert not write_session.in_transaction()
    finally:
        prepared.close()
        _dispose_caller_session(write_session)


@pytest.mark.parametrize("protected_stage", ("claim", "catalog_resolve", "execute"))
def test_nested_savepoint_is_never_valid_issuance_transaction_provenance(
    tmp_path: Any,
    protected_stage: str,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    write_session = _open_unbegun_write_session(backend, "nested-provenance")
    lease = components.pending_identity_verifier_port.open_issuance_lease(
        write_session,
        prepared,
    )
    evidence = components.pending_identity_verifier_port.locked_recheck(
        write_session,
        lease,
        prepared,
    )
    proof = None
    handle = None
    if protected_stage != "claim":
        claim = components.pending_identity_verifier_port.bind_claim(
            write_session,
            lease,
            evidence,
        )
        proof = components.proof_issuer.issue_after_claim(
            write_session,
            lease,
            claim,
            prepared,
        )
        if protected_stage == "execute":
            handle = components.catalog.resolve_server_loaded(proof)
    root_transaction = write_session.get_transaction()
    nested = write_session.begin_nested()
    try:
        assert write_session.in_nested_transaction()
        assert write_session.get_transaction() is root_transaction
        with pytest.raises(
            (TypeError, ValueError),
            match="nested|savepoint|transaction|provenance|closed|revoked",
        ):
            if protected_stage == "claim":
                components.pending_identity_verifier_port.bind_claim(
                    write_session,
                    lease,
                    evidence,
                )
            elif protected_stage == "catalog_resolve":
                components.catalog.resolve_server_loaded(proof)
            else:
                context = _execution_context(backend, write_session)
                components.proof_consumer_port.execute(handle, context)
        assert _EXECUTION_EVENTS == []
        assert components.pending_identity_verifier_port._issuance == {}
    finally:
        if nested.is_active:
            nested.rollback()
        _close_issuance(lease, write_session)


def test_raw_transaction_control_cannot_create_db_transaction_aba(
    tmp_path: Any,
) -> None:
    from sqlalchemy import text

    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    proof, lease, write_session = _issue(components, backend, prepared)

    try:
        with pytest.raises(
            (TypeError, ValueError),
            match="transaction|control|provenance|BEGIN|COMMIT",
        ):
            write_session.execute(text("COMMIT"))
        with pytest.raises(
            (TypeError, ValueError),
            match="transaction|closed|revoked|provenance",
        ):
            components.catalog.resolve_server_loaded(proof)
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


def test_transaction_control_rejects_leading_empty_statement_bypass(
    tmp_path: Any,
) -> None:
    from sqlalchemy import text

    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    proof, lease, write_session = _issue(components, backend, prepared)

    try:
        with pytest.raises(
            (TypeError, ValueError),
            match="transaction|control|provenance|BEGIN|COMMIT",
        ):
            write_session.execute(text("; COMMIT"))
        with pytest.raises(
            (TypeError, ValueError),
            match="transaction|closed|revoked|provenance",
        ):
            components.catalog.resolve_server_loaded(proof)
        assert components.proof_registry._proofs == {}
        assert components.proof_registry._handles == {}
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


@pytest.mark.parametrize("protected_stage", ("catalog_resolve", "execute"))
def test_direct_dbapi_commit_is_blocked_before_catalog_or_executor(
    tmp_path: Any,
    protected_stage: str,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    proof, lease, write_session = _issue(components, backend, prepared)
    handle = None
    context = None
    if protected_stage == "execute":
        handle = components.catalog.resolve_server_loaded(proof)
        context = _execution_context(backend, write_session)
    state = components.pending_identity_verifier_port._issuance[lease]
    driver_connection = state.connection.connection.driver_connection

    try:
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            driver_connection.commit()
        assert driver_connection.in_transaction
        with pytest.raises(
            (TypeError, ValueError),
            match="transaction|closed|revoked|provenance|ABA",
        ):
            if protected_stage == "catalog_resolve":
                components.catalog.resolve_server_loaded(proof)
            else:
                components.proof_consumer_port.execute(handle, context)
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


@pytest.mark.parametrize("transaction_end", ("commit", "rollback"))
def test_aborted_sqlalchemy_transaction_event_cannot_arm_direct_dbapi_end(
    tmp_path: Any,
    transaction_end: str,
) -> None:
    from sqlalchemy import event

    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    proof, lease, write_session = _issue(components, backend, prepared)
    state = components.pending_identity_verifier_port._issuance[lease]
    driver_connection = state.connection.connection.driver_connection

    def abort_transaction_end(_connection: object) -> None:
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            getattr(driver_connection, transaction_end)()
        raise RuntimeError("abort transaction end")

    event.listen(state.connection, transaction_end, abort_transaction_end)
    try:
        with pytest.raises(RuntimeError, match="abort transaction end"):
            getattr(write_session, transaction_end)()
        event.remove(state.connection, transaction_end, abort_transaction_end)
        with pytest.raises(sqlite3.DatabaseError, match="authorized|closed database"):
            getattr(driver_connection, transaction_end)()
        with pytest.raises(
            (TypeError, ValueError),
            match="transaction|closed|revoked|provenance|control",
        ):
            components.catalog.resolve_server_loaded(proof)
        assert _EXECUTION_EVENTS == []
    finally:
        if event.contains(state.connection, transaction_end, abort_transaction_end):
            event.remove(state.connection, transaction_end, abort_transaction_end)
        _close_issuance(lease, write_session)


def test_executor_cannot_end_the_caller_transaction_while_lease_is_live(
    tmp_path: Any,
) -> None:
    from sqlalchemy import text

    from offerpilot.ai.write_operations import WriteOperationError

    global _EXECUTION_OBSERVER

    components, backend, _key = _components(tmp_path)
    with backend.session_factory.begin() as seed_session:
        seed_session.execute(
            text(
                "INSERT INTO applications "
                "(id, company_name, position_name, status, source) "
                "VALUES (7, 'Executor Fence Co', 'Engineer', 'interview', 'test')"
            )
        )
    _prepared, _proof, handle, lease_and_session = _issue_route(components, backend)
    lease, write_session = lease_and_session
    context = _execution_context(backend, write_session)

    def attempt_commit(_encoded_args: str, execution_context: Any) -> None:
        execution_context.session.execute(text("UPDATE applications SET status='offer' WHERE id=7"))
        execution_context.session.commit()

    _EXECUTION_OBSERVER = attempt_commit
    try:
        with pytest.raises(WriteOperationError, match="operation_not_committed"):
            components.proof_consumer_port.execute(handle, context)
        assert not write_session.in_transaction()
        assert _EXECUTION_EVENTS == [("executor", CANONICAL_ARGS, context)]
    finally:
        _EXECUTION_OBSERVER = None
        _close_issuance(lease, write_session)

    with backend.session_factory() as verification_session:
        status = verification_session.scalar(text("SELECT status FROM applications WHERE id=7"))
    assert status == "interview"


@pytest.mark.parametrize(
    "projection_action",
    (
        "commit",
        "rollback",
        "close",
        "business_dml",
        "raw_business_dml",
        "forbidden_journal_dml",
        "raw_savepoint",
        "sa_rollback_guard",
        "sa_release_guard",
    ),
)
def test_bound_projection_cannot_take_ledger_transaction_or_business_write_authority(
    tmp_path: Any,
    projection_action: str,
) -> None:
    from sqlalchemy import text

    from offerpilot.ai.write_operations import WriteOperationError

    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    with backend.session_factory.begin() as seed_session:
        seed_session.execute(
            text(
                "INSERT INTO applications "
                "(id, company_name, position_name, status, source) "
                "VALUES (7, 'Projection Fence Co', 'Engineer', 'interview', 'test')"
            )
        )
    _prepared, _proof, handle, lease_and_session = _issue_route(components, backend)
    lease, write_session = lease_and_session
    context = _execution_context(backend, write_session)
    projection_events: list[str] = []

    def hostile_projection() -> None:
        projection_events.append("projection")
        if projection_action == "business_dml":
            write_session.execute(text("UPDATE applications SET status='offer' WHERE id=7"))
        elif projection_action == "raw_business_dml":
            state = components.pending_identity_verifier_port._issuance[lease]
            state.dbapi_connection.execute("UPDATE applications SET status='offer' WHERE id=7")
        elif projection_action == "forbidden_journal_dml":
            write_session.execute(text("UPDATE agent_events SET event_type=event_type WHERE 0"))
        elif projection_action == "raw_savepoint":
            state = components.pending_identity_verifier_port._issuance[lease]
            state.dbapi_connection.execute("SAVEPOINT attacker_nested")
        elif projection_action == "sa_rollback_guard":
            state = components.pending_identity_verifier_port._issuance[lease]
            state.connection._rollback_to_savepoint_impl(state.transaction_guard_name)
        elif projection_action == "sa_release_guard":
            state = components.pending_identity_verifier_port._issuance[lease]
            state.connection._release_savepoint_impl(state.transaction_guard_name)
        else:
            write_session.execute(text("UPDATE agent_runs SET status=status WHERE 0"))
            getattr(write_session, projection_action)()
        projection_events.append("authority_escaped")

    try:
        with pytest.raises(WriteOperationError, match="operation_not_committed"):
            components.proof_consumer_port.execute(
                handle,
                context,
                before_execute=hostile_projection,
            )
        assert projection_events == ["projection"]
        assert _EXECUTION_EVENTS == []
        assert not write_session.in_transaction()
    finally:
        _close_issuance(lease, write_session)

    with backend.session_factory() as verification_session:
        status = verification_session.scalar(text("SELECT status FROM applications WHERE id=7"))
    assert status == "interview"


@pytest.mark.parametrize("protected_stage", ("catalog_resolve", "execute"))
def test_direct_dbapi_savepoint_control_fails_before_catalog_or_executor(
    tmp_path: Any,
    protected_stage: str,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    proof, lease, write_session = _issue(components, backend, prepared)
    handle = None
    context = None
    if protected_stage == "execute":
        handle = components.catalog.resolve_server_loaded(proof)
        context = _execution_context(backend, write_session)
    state = components.pending_identity_verifier_port._issuance[lease]
    driver_connection = state.connection.connection.driver_connection

    try:
        for statement in (
            "SAVEPOINT attacker_nested",
            "ROLLBACK TO attacker_nested",
            "RELEASE attacker_nested",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="authorized"):
                driver_connection.execute(statement)
        with pytest.raises(
            (TypeError, ValueError),
            match="transaction|closed|revoked|provenance|control",
        ):
            if protected_stage == "catalog_resolve":
                components.catalog.resolve_server_loaded(proof)
            else:
                components.proof_consumer_port.execute(handle, context)
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


def test_post_registration_baseexception_removes_transaction_listener_and_closure(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import event

    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, "listener-registration-abort")
    route_module = _route_module()
    original_listen = route_module.event.listen
    captured: list[tuple[object, str, object]] = []

    def listen_then_abort(
        target: object,
        identifier: str,
        listener: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        original_listen(target, identifier, listener, *args, **kwargs)
        captured.append((target, identifier, listener))
        assert event.contains(target, identifier, listener)
        raise _NonLocalAbort()

    monkeypatch.setattr(route_module.event, "listen", listen_then_abort)
    try:
        with pytest.raises(_NonLocalAbort):
            components.pending_identity_verifier_port.open_issuance_lease(
                write_session,
                prepared,
            )
        assert len(captured) == 1
        target, identifier, listener = captured[0]
        assert not event.contains(target, identifier, listener)
        assert components.pending_identity_verifier_port._issuance == {}
        assert components.pending_identity_verifier_port._evidence == {}
        assert _retained_argument_material(components) == []
    finally:
        for target, identifier, listener in captured:
            if event.contains(target, identifier, listener):
                event.remove(target, identifier, listener)
        prepared.close()
        _dispose_caller_session(write_session)


@pytest.mark.parametrize("drift", ("issuance_lease", "handle"))
def test_lease_close_clears_proof_graph_even_after_entry_cleanup_key_drift(
    tmp_path: Any,
    drift: str,
) -> None:
    components, backend, _key = _components(tmp_path)
    _prepared, proof, handle, lease_and_session = _issue_route(components, backend)
    lease, write_session = lease_and_session
    entry = components.proof_registry._proofs[proof]
    if drift == "issuance_lease":
        entry.issuance_lease = object()
    else:
        entry.handle = object()

    try:
        lease.close()
        assert components.proof_registry._proofs == {}
        assert components.proof_registry._handles == {}
        assert handle not in components.proof_registry._handles
    finally:
        components.proof_registry._proofs.clear()
        components.proof_registry._handles.clear()
        _dispose_caller_session(write_session)


@pytest.mark.parametrize(
    "drift",
    (
        "cleanup_clear_reopen",
        "cleanup_replace_reopen",
        "cleanup_none_reopen",
        "status_closed",
    ),
)
def test_issuance_lease_cleanup_and_status_drift_still_revokes_every_root(
    tmp_path: Any,
    drift: str,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _proof, handle, lease_and_session = _issue_route(components, backend)
    lease, write_session = lease_and_session
    context = _execution_context(backend, write_session)
    verifier = components.pending_identity_verifier_port
    preparation_registry = components.preparation_registry
    proof_registry = components.proof_registry

    if drift == "cleanup_clear_reopen":
        lease._cleanup.clear()
    elif drift == "cleanup_replace_reopen":
        object.__setattr__(lease, "_cleanup", [])
    elif drift == "cleanup_none_reopen":
        object.__setattr__(lease, "_cleanup", None)
    else:
        object.__setattr__(lease, "_status", "closed")

    try:
        lease.close()
        if drift.endswith("reopen"):
            object.__setattr__(lease, "_status", "open")
        with pytest.raises(
            (TypeError, ValueError),
            match="integrity|closed|revoked|lease|authority|transaction",
        ):
            components.proof_consumer_port.execute(handle, context)
        assert verifier._evidence == {}
        assert verifier._issuance == {}
        assert preparation_registry._prepared == {}
        assert preparation_registry._entries == {}
        assert proof_registry._proofs == {}
        assert proof_registry._handles == {}
        assert _EXECUTION_EVENTS == []
    finally:
        prepared.close()
        _dispose_caller_session(write_session)


@pytest.mark.parametrize("registry_name", ("_evidence", "_issuance"))
def test_verifier_map_replacement_cleanup_clears_original_and_current_roots(
    tmp_path: Any,
    registry_name: str,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    write_session = _open_unbegun_write_session(backend, f"retained-{registry_name}")
    verifier = components.pending_identity_verifier_port
    preparation_registry = components.preparation_registry
    proof_registry = components.proof_registry
    prepared_entry = preparation_registry._prepared[prepared]
    lease = verifier.open_issuance_lease(write_session, prepared)
    verifier.locked_recheck(write_session, lease, prepared)
    original_map = getattr(verifier, registry_name)
    replacement_map = dict(original_map)
    object.__setattr__(verifier, registry_name, replacement_map)

    try:
        lease.close()
        assert original_map == {}
        assert replacement_map == {}
        assert preparation_registry._prepared == {}
        assert preparation_registry._entries == {}
        assert prepared_entry.raw_args is None
        assert prepared_entry.effective_args is None
        assert prepared_entry.metadata is None
        assert proof_registry._proofs == {}
        assert proof_registry._handles == {}
    finally:
        prepared.close()
        _dispose_caller_session(write_session)


@pytest.mark.parametrize("topology_field", ("_lock", "_preparation_registry"))
def test_verifier_topology_drift_still_cleans_sealed_attempt_roots(
    tmp_path: Any,
    topology_field: str,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    write_session = _open_unbegun_write_session(backend, f"drift-{topology_field}")
    verifier = components.pending_identity_verifier_port
    preparation_registry = components.preparation_registry
    prepared_entry = preparation_registry._prepared[prepared]
    lease = verifier.open_issuance_lease(write_session, prepared)
    verifier.locked_recheck(write_session, lease, prepared)
    evidence_map = verifier._evidence
    issuance_map = verifier._issuance
    object.__setattr__(verifier, topology_field, None)

    try:
        lease.close()
        assert evidence_map == {}
        assert issuance_map == {}
        assert preparation_registry._prepared == {}
        assert preparation_registry._entries == {}
        assert prepared_entry.raw_args is None
        assert prepared_entry.effective_args is None
        assert prepared_entry.metadata is None
    finally:
        prepared.close()
        _dispose_caller_session(write_session)


@pytest.mark.parametrize("registry_name", ("_proofs", "_handles"))
def test_proof_registry_map_replacement_cleanup_clears_every_sealed_root(
    tmp_path: Any,
    registry_name: str,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _proof, _handle, lease_and_session = _issue_route(components, backend)
    lease, write_session = lease_and_session
    proof_registry = components.proof_registry
    original_map = getattr(proof_registry, registry_name)
    replacement_map = dict(original_map)
    object.__setattr__(proof_registry, registry_name, replacement_map)

    try:
        lease.close()
        assert original_map == {}
        assert replacement_map == {}
        assert proof_registry._proofs == {}
        assert proof_registry._handles == {}
    finally:
        prepared.close()
        _dispose_caller_session(write_session)


def test_preparation_registry_map_replacement_clears_sealed_argument_roots(
    tmp_path: Any,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _proof, _handle, lease_and_session = _issue_route(components, backend)
    lease, write_session = lease_and_session
    preparation_registry = components.preparation_registry
    original_entries = preparation_registry._entries
    original_prepared = preparation_registry._prepared
    entry = original_prepared[prepared]
    assert entry.effective_args is not None
    replacement_entries: dict[object, object] = {}
    replacement_prepared: dict[object, object] = {}
    object.__setattr__(preparation_registry, "_entries", replacement_entries)
    object.__setattr__(preparation_registry, "_prepared", replacement_prepared)

    try:
        lease.close()
        assert original_entries == {}
        assert original_prepared == {}
        assert replacement_entries == {}
        assert replacement_prepared == {}
        assert entry.raw_args is None
        assert entry.effective_args is None
        assert entry.metadata is None
        assert entry.status == "revoked"
    finally:
        prepared.close()
        _dispose_caller_session(write_session)


@pytest.mark.parametrize(
    "drift",
    (
        "evidence_map_identity",
        "issuance_map_identity",
        "copied_evidence_state",
        "copied_issuance_state",
    ),
)
def test_verifier_registry_and_state_identity_replacement_fail_before_claim(
    tmp_path: Any,
    drift: str,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    write_session = _open_unbegun_write_session(backend, f"verifier-drift-{drift}")
    verifier = components.pending_identity_verifier_port
    proof_registry = components.proof_registry
    lease = verifier.open_issuance_lease(write_session, prepared)
    evidence = verifier.locked_recheck(write_session, lease, prepared)
    if drift == "evidence_map_identity":
        object.__setattr__(verifier, "_evidence", dict(verifier._evidence))
    elif drift == "issuance_map_identity":
        object.__setattr__(verifier, "_issuance", dict(verifier._issuance))
    elif drift == "copied_evidence_state":
        verifier._evidence[evidence] = replace(verifier._evidence[evidence])
    else:
        verifier._issuance[lease] = replace(verifier._issuance[lease])

    try:
        with pytest.raises(
            (TypeError, ValueError),
            match="integrity|identity|registry|state|drift|forged",
        ):
            verifier.bind_claim(write_session, lease, evidence)
        assert backend.events == ["read_snapshot", "locked_mutable_recheck"]
        assert proof_registry._proofs == {}
        assert proof_registry._handles == {}
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


def test_copied_issuance_state_cannot_rebind_proof_to_a_new_transaction(
    tmp_path: Any,
) -> None:
    from sqlalchemy import event

    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    proof, lease, write_session = _issue(components, backend, prepared)
    verifier = components.pending_identity_verifier_port
    original_state = verifier._issuance[lease]
    original_connection = original_state.connection
    listener = original_state.transaction_control_listener
    rebound_session = _open_caller_session(backend, "copied-state-rebound")
    rebound_connection = rebound_session.connection()

    try:
        verifier._issuance[lease] = replace(
            original_state,
            session=rebound_session,
            transaction=rebound_session.get_transaction(),
            connection=rebound_connection,
            connection_transaction=rebound_connection.get_transaction(),
            dbapi_connection=rebound_connection.connection.driver_connection,
        )
        with pytest.raises(
            (TypeError, ValueError),
            match="transaction|provenance|identity|state|drift|revoked",
        ):
            components.catalog.resolve_server_loaded(proof)
        assert components.proof_registry._proofs == {}
        assert components.proof_registry._handles == {}
        assert _EXECUTION_EVENTS == []
    finally:
        lease.close()
        if listener is not None and event.contains(
            original_connection,
            "before_cursor_execute",
            listener,
        ):
            event.remove(original_connection, "before_cursor_execute", listener)
        _dispose_caller_session(rebound_session)
        _dispose_caller_session(write_session)


def test_issuance_close_uses_only_sealed_cleanup_identity_after_live_state_drift(
    tmp_path: Any,
) -> None:
    from sqlalchemy import event

    components, backend, _key = _components(tmp_path / "source")
    _foreign_components, foreign_backend, _foreign_key = _components(tmp_path / "foreign")
    prepared, _read_session = _prepare(components, backend)
    foreign_prepared, _foreign_read_session = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, "sealed-cleanup-source")
    foreign_session = _open_unbegun_write_session(
        foreign_backend,
        "sealed-cleanup-foreign",
    )
    verifier = components.pending_identity_verifier_port
    lease = verifier.open_issuance_lease(write_session, prepared)
    foreign_lease = verifier.open_issuance_lease(foreign_session, foreign_prepared)
    state = verifier._issuance[lease]
    foreign_state = verifier._issuance[foreign_lease]
    sealed_fence = state.transaction_control_fence
    foreign_listener = foreign_state.transaction_control_listener
    sealed_fence._violate()

    for field_name in (
        "session",
        "transaction",
        "connection",
        "connection_transaction",
        "transaction_guard_name",
        "transaction_control_fence",
        "dbapi_connection",
        "transaction_control_authorizer",
        "session_token",
        "prepared_call",
        "transaction_control_listener",
        "transaction_control_savepoint_listeners",
    ):
        setattr(state, field_name, getattr(foreign_state, field_name))

    foreign_evidence = None
    try:
        lease.close()

        assert sealed_fence._boundary_closed is True
        assert not write_session.in_transaction()
        assert foreign_state.transaction_control_fence._boundary_closed is False
        assert foreign_session.in_transaction()
        assert event.contains(
            foreign_state.connection,
            "before_cursor_execute",
            foreign_listener,
        )
        assert foreign_prepared in components.preparation_registry._prepared
        foreign_evidence = verifier.locked_recheck(
            foreign_session,
            foreign_lease,
            foreign_prepared,
        )
    finally:
        if foreign_evidence is not None:
            verifier._revoke_evidence(foreign_evidence)
        verifier._close_issuance(lease)
        foreign_lease.close()
        prepared.close()
        foreign_prepared.close()
        _dispose_caller_session(foreign_session)
        _dispose_caller_session(write_session)


@pytest.mark.parametrize("drift_kind", ("foreign_registry_state", "foreign_seal"))
def test_invalid_issuance_cleanup_identity_cannot_close_a_foreign_lease(
    tmp_path: Any,
    drift_kind: str,
) -> None:
    from sqlalchemy import event

    components, backend, _key = _components(tmp_path / "source")
    _foreign_components, foreign_backend, _foreign_key = _components(tmp_path / "foreign")
    prepared, _read_session = _prepare(components, backend)
    foreign_prepared, _foreign_read_session = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, "invalid-cleanup-source")
    foreign_session = _open_unbegun_write_session(
        foreign_backend,
        "invalid-cleanup-foreign",
    )
    verifier = components.pending_identity_verifier_port
    lease = verifier.open_issuance_lease(write_session, prepared)
    foreign_lease = verifier.open_issuance_lease(foreign_session, foreign_prepared)
    state = verifier._issuance[lease]
    foreign_state = verifier._issuance[foreign_lease]
    foreign_listener = foreign_state.transaction_control_listener
    if drift_kind == "foreign_registry_state":
        verifier._issuance[lease] = foreign_state
    else:
        state.integrity_seal = foreign_state.integrity_seal
        for field_name in (
            "session",
            "transaction",
            "connection",
            "connection_transaction",
            "transaction_guard_name",
            "transaction_control_fence",
            "dbapi_connection",
            "transaction_control_authorizer",
            "session_token",
            "prepared_call",
            "transaction_control_listener",
            "transaction_control_savepoint_listeners",
        ):
            setattr(state, field_name, getattr(foreign_state, field_name))

    foreign_evidence = None
    try:
        lease.close()

        assert foreign_state.transaction_control_fence._boundary_closed is False
        assert foreign_session.in_transaction()
        assert event.contains(
            foreign_state.connection,
            "before_cursor_execute",
            foreign_listener,
        )
        assert foreign_prepared in components.preparation_registry._prepared
        foreign_evidence = verifier.locked_recheck(
            foreign_session,
            foreign_lease,
            foreign_prepared,
        )
    finally:
        if foreign_evidence is not None:
            verifier._revoke_evidence(foreign_evidence)
        verifier._close_issuance(lease)
        foreign_lease.close()
        prepared.close()
        foreign_prepared.close()
        _dispose_caller_session(foreign_session)
        _dispose_caller_session(write_session)


@pytest.mark.parametrize("drift_kind", ("foreign_callbacks", "foreign_seal"))
def test_lease_close_uses_only_its_own_sealed_callback_roots(
    tmp_path: Any,
    drift_kind: str,
) -> None:
    from sqlalchemy import event

    components, backend, _key = _components(tmp_path / "source")
    _foreign_components, foreign_backend, _foreign_key = _components(tmp_path / "foreign")
    prepared, _read_session = _prepare(components, backend)
    foreign_prepared, _foreign_read_session = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, "lease-cleanup-source")
    foreign_session = _open_unbegun_write_session(
        foreign_backend,
        "lease-cleanup-foreign",
    )
    verifier = components.pending_identity_verifier_port
    lease = verifier.open_issuance_lease(write_session, prepared)
    foreign_lease = verifier.open_issuance_lease(foreign_session, foreign_prepared)
    foreign_state = verifier._issuance[foreign_lease]
    foreign_listener = foreign_state.transaction_control_listener
    if drift_kind == "foreign_callbacks":
        lease._cleanup.extend(foreign_lease._cleanup)
        object.__setattr__(lease, "_cleanup_roots", tuple(lease._cleanup))
    else:
        object.__setattr__(lease, "_integrity_seal", foreign_lease._integrity_seal)

    foreign_evidence = None
    try:
        lease.close()

        assert foreign_state.transaction_control_fence._boundary_closed is False
        assert foreign_session.in_transaction()
        assert event.contains(
            foreign_state.connection,
            "before_cursor_execute",
            foreign_listener,
        )
        assert foreign_prepared in components.preparation_registry._prepared
        foreign_evidence = verifier.locked_recheck(
            foreign_session,
            foreign_lease,
            foreign_prepared,
        )
    finally:
        if foreign_evidence is not None:
            verifier._revoke_evidence(foreign_evidence)
        verifier._close_issuance(lease)
        foreign_lease.close()
        prepared.close()
        foreign_prepared.close()
        _dispose_caller_session(foreign_session)
        _dispose_caller_session(write_session)


def test_invalid_evidence_seal_cannot_revoke_a_foreign_prepared_call(
    tmp_path: Any,
) -> None:
    components, backend, _key = _components(tmp_path / "source")
    _foreign_components, foreign_backend, _foreign_key = _components(tmp_path / "foreign")
    prepared, _read_session = _prepare(components, backend)
    foreign_prepared, _foreign_read_session = _prepare(components, backend)
    write_session = _open_unbegun_write_session(backend, "evidence-cleanup-source")
    foreign_session = _open_unbegun_write_session(
        foreign_backend,
        "evidence-cleanup-foreign",
    )
    verifier = components.pending_identity_verifier_port
    lease = verifier.open_issuance_lease(write_session, prepared)
    foreign_lease = verifier.open_issuance_lease(foreign_session, foreign_prepared)
    evidence = verifier.locked_recheck(write_session, lease, prepared)
    foreign_evidence = verifier.locked_recheck(
        foreign_session,
        foreign_lease,
        foreign_prepared,
    )
    evidence_state = verifier._evidence[evidence]
    foreign_evidence_state = verifier._evidence[foreign_evidence]
    evidence_state.integrity_seal = foreign_evidence_state.integrity_seal
    evidence_state.prepared_call = foreign_prepared

    try:
        lease.close()

        assert foreign_prepared in components.preparation_registry._prepared
        assert verifier._evidence[foreign_evidence] is foreign_evidence_state
        verifier._require_issuance_transaction(foreign_lease)
    finally:
        verifier._revoke_evidence(evidence)
        verifier._revoke_evidence(foreign_evidence)
        foreign_lease.close()
        prepared.close()
        foreign_prepared.close()
        _dispose_caller_session(foreign_session)
        _dispose_caller_session(write_session)


def test_lease_close_cleans_all_attempt_state_after_issuance_map_is_cleared(
    tmp_path: Any,
) -> None:
    from sqlalchemy import event

    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    write_session = _open_unbegun_write_session(backend, "cleared-issuance-map")
    verifier = components.pending_identity_verifier_port
    lease = verifier.open_issuance_lease(write_session, prepared)
    evidence = verifier.locked_recheck(write_session, lease, prepared)
    state = verifier._issuance[lease]
    listener = state.transaction_control_listener
    assert _retained_argument_material(components)
    verifier._issuance.clear()

    try:
        lease.close()
        assert verifier._evidence == {}
        assert components.preparation_registry._prepared == {}
        assert components.preparation_registry._entries == {}
        assert _retained_argument_material(components) == []
        assert state.transaction_control_fence._boundary_closed is True
        assert state.transaction_control_fence._expected_savepoint_kind is None
        if listener is not None:
            assert not event.contains(state.connection, "before_cursor_execute", listener)
    finally:
        if lease not in verifier._issuance:
            verifier._issuance[lease] = state
        verifier._close_issuance(lease)
        verifier._revoke_evidence(evidence)
        prepared.close()
        if listener is not None and event.contains(
            state.connection,
            "before_cursor_execute",
            listener,
        ):
            event.remove(state.connection, "before_cursor_execute", listener)
        _dispose_caller_session(write_session)


def test_rebound_cross_lease_evidence_state_cannot_authorize_target_claim(
    tmp_path: Any,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path / "source")
    _target_components, target_backend, _target_key = _components(tmp_path / "target")
    backend.snapshot = backend._snapshot(claimed=False)
    source_prepared, _read = _prepare(components, backend)
    backend.snapshot = backend._snapshot(claimed=False)
    target_prepared, _read = _prepare(components, backend)
    source_session = _open_unbegun_write_session(backend, "forged-source-lease")
    target_session = _open_unbegun_write_session(target_backend, "forged-target-lease")
    verifier = components.pending_identity_verifier_port
    source_lease = verifier.open_issuance_lease(source_session, source_prepared)
    target_lease = verifier.open_issuance_lease(target_session, target_prepared)
    source_evidence = verifier.locked_recheck(
        source_session,
        source_lease,
        source_prepared,
    )
    backend.snapshot = backend._snapshot(claimed=False)
    target_evidence = verifier.locked_recheck(
        target_session,
        target_lease,
        target_prepared,
    )
    target_state = verifier._issuance[target_lease]
    source_evidence_state = verifier._evidence[source_evidence]
    forged_evidence_state = replace(
        source_evidence_state,
        session=target_session,
        transaction=target_state.transaction,
        issuance_lease=target_lease,
        prepared_call=target_prepared,
    )
    verifier._evidence[source_evidence] = forged_evidence_state
    target_state.locked_evidence = source_evidence
    target_state.locked_snapshot = forged_evidence_state.snapshot
    before = tuple(backend.events)

    try:
        with pytest.raises(
            (TypeError, ValueError),
            match="evidence|identity|registry|state|drift|forged|lease",
        ):
            verifier.bind_claim(target_session, target_lease, source_evidence)
        assert tuple(backend.events) == before
        assert components.proof_registry._proofs == {}
        assert components.proof_registry._handles == {}
        assert _EXECUTION_EVENTS == []
    finally:
        verifier._revoke_evidence(target_evidence)
        _close_issuance(target_lease, target_session)
        _close_issuance(source_lease, source_session)


def test_executor_rejects_context_bound_to_a_different_live_session(
    tmp_path: Any,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    _prepared, _proof, handle, lease_and_session = _issue_route(components, backend)
    lease, write_session = lease_and_session
    other_session = _open_caller_session(backend, "cross-session-execution")
    other_context = _execution_context(backend, other_session)

    try:
        with pytest.raises(
            (TypeError, ValueError),
            match="Session|transaction|provenance|identity",
        ):
            components.proof_consumer_port.execute(handle, other_context)
        assert _EXECUTION_EVENTS == []
        assert write_session.in_transaction()
        assert other_session.in_transaction()
    finally:
        _dispose_caller_session(other_session)
        _close_issuance(lease, write_session)


def test_consumed_preparation_clears_persisted_raw_args_immediately(
    tmp_path: Any,
) -> None:
    components, backend, _key = _components(tmp_path)
    prepared, _read_session = _prepare(components, backend)
    _proof, lease, write_session = _issue(components, backend, prepared)

    try:
        retained = _retained_argument_material(components)
        assert len(retained) == 1
        raw_args, effective_args = retained[0]
        assert raw_args is None
        assert effective_args == CANONICAL_ARGS
    finally:
        _close_issuance(lease, write_session)


@pytest.mark.parametrize(
    "exit_kind",
    ("commit", "rollback", "exception", "cancellation", "base_exception"),
)
def test_issuance_cleanup_revokes_proof_handle_and_clears_effective_args(
    tmp_path: Any,
    exit_kind: str,
) -> None:
    components, backend, _key = _components(tmp_path)
    _prepared, proof, handle, lease_and_session = _issue_route(components, backend)
    lease, _write_session = lease_and_session
    context = _execution_context(backend, _write_session)

    if exit_kind == "commit":
        lease.close(outcome="commit")
    elif exit_kind == "rollback":
        lease.close(outcome="rollback")
    elif exit_kind == "exception":
        with pytest.raises(RuntimeError):
            with lease:
                raise RuntimeError("boom")
    elif exit_kind == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            with lease:
                raise asyncio.CancelledError()
    else:
        with pytest.raises(_NonLocalAbort):
            with lease:
                raise _NonLocalAbort()

    with pytest.raises((TypeError, ValueError), match="revoked|closed|expired"):
        components.catalog.resolve_server_loaded(proof)
    with pytest.raises((TypeError, ValueError), match="revoked|closed|expired"):
        components.proof_consumer_port.execute(handle, context)
    assert _EXECUTION_EVENTS == []
    _dispose_caller_session(_write_session)


def test_effective_arguments_have_one_owner_and_exist_only_during_executor_window(
    tmp_path: Any,
) -> None:
    global _EXECUTION_OBSERVER

    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, _proof, handle, lease_and_session = _issue_route(components, backend)
    lease, _write_session = lease_and_session
    context = _execution_context(backend, _write_session)
    entry = components.preparation_registry._prepared[prepared]

    for value in (components.proof_registry, components.catalog, handle):
        public_names = {name for name in dir(value) if not name.startswith("_")}
        assert "effective_args" not in public_names
        assert "encoded_args" not in public_names
    assert not hasattr(handle, "effective_args")
    assert not hasattr(handle, "encoded_args")
    assert not hasattr(handle, "execute")
    assert not hasattr(handle, "adapter")
    assert not hasattr(handle, "callable")

    def assert_executor_window(encoded_args: str, _context: object) -> None:
        assert entry.status == "executing"
        assert entry.effective_args is encoded_args
        assert entry.raw_args is None
        assert components.preparation_registry._prepared[prepared] is entry

    _EXECUTION_OBSERVER = assert_executor_window
    try:
        result = components.proof_consumer_port.execute(
            handle,
            context,
        )
    finally:
        _EXECUTION_OBSERVER = None

    assert result == '{"ok":true}'
    assert len(_EXECUTION_EVENTS) == 1
    assert json.loads(_EXECUTION_EVENTS[0][1]) == json.loads(RAW_ARGS)
    with pytest.raises((TypeError, ValueError), match="once|consumed|cleared|revoked"):
        components.proof_consumer_port.execute(handle, context)
    assert len(_EXECUTION_EVENTS) == 1
    registry = components.preparation_registry
    assert registry._prepared == {prepared: entry}
    assert registry._entries == {entry.binding: entry}
    assert entry.status == "cleared"
    assert entry.raw_args is None
    assert entry.effective_args is None
    assert entry.metadata is None
    assert entry.prepared_call is prepared
    assert entry.issuance_lease is lease
    with pytest.raises((TypeError, ValueError), match="consumed|cleared|revoked"):
        registry._inspect_prepared(prepared)
    _close_issuance(lease, _write_session)
    _assert_prepared_attempt_is_cleared(components, prepared, entry)


@pytest.mark.parametrize(
    "drift_kind",
    (
        "effective_args",
        "prepared_map",
        "proofs_map",
        "handles_map",
        "proof_safe_metadata",
    ),
)
def test_post_proof_entry_or_registry_drift_fails_before_executor(
    tmp_path: Any,
    drift_kind: str,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    prepared, proof, handle, lease_and_session = _issue_route(components, backend)
    lease, write_session = lease_and_session
    preparation_entry = components.preparation_registry._prepared[prepared]
    proof_entry = components.proof_registry._proofs[proof]
    if drift_kind == "effective_args":
        preparation_entry.effective_args = '{"application_id":999,"jd_text":"tampered"}'
    elif drift_kind == "prepared_map":
        object.__setattr__(
            components.preparation_registry,
            "_prepared",
            dict(components.preparation_registry._prepared),
        )
    elif drift_kind == "proofs_map":
        object.__setattr__(
            components.proof_registry,
            "_proofs",
            dict(components.proof_registry._proofs),
        )
    elif drift_kind == "handles_map":
        object.__setattr__(
            components.proof_registry,
            "_handles",
            dict(components.proof_registry._handles),
        )
    else:
        proof_entry.safe_metadata = MappingProxyType(dict(proof_entry.safe_metadata))

    try:
        with pytest.raises(
            (TypeError, ValueError),
            match="integrity|drift|identity|cleared|revoked|prepared|Registry",
        ):
            components.proof_consumer_port.execute(
                handle,
                _execution_context(backend, write_session),
            )
        assert _EXECUTION_EVENTS == []
    finally:
        _close_issuance(lease, write_session)


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("borrow failed"), asyncio.CancelledError(), _NonLocalAbort()),
    ids=("exception", "cancellation", "base_exception"),
)
def test_borrow_baseexception_clears_arguments_and_keeps_handle_one_shot(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    _prepared, _proof, handle, lease_and_session = _issue_route(components, backend)
    lease, write_session = lease_and_session
    context = _execution_context(backend, write_session)
    registry_type = type(components.preparation_registry)
    original_borrow = registry_type._borrow_for_execute

    def fail_borrow(*_args: object, **_kwargs: object) -> str:
        raise failure

    monkeypatch.setattr(registry_type, "_borrow_for_execute", fail_borrow)
    try:
        with pytest.raises(type(failure)):
            components.proof_consumer_port.execute(handle, context)

        monkeypatch.setattr(registry_type, "_borrow_for_execute", original_borrow)
        with pytest.raises((TypeError, ValueError), match="once|cleared|consumed|revoked"):
            components.proof_consumer_port.execute(handle, context)

        assert _EXECUTION_EVENTS == []
        assert _retained_argument_material(components) == []
    finally:
        _close_issuance(lease, write_session)


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("executor failed"), asyncio.CancelledError(), _NonLocalAbort()),
    ids=("exception", "cancellation", "base_exception"),
)
def test_executor_baseexception_clears_args_in_finally_and_never_reexecutes(
    tmp_path: Any,
    failure: BaseException,
) -> None:
    global _EXECUTION_FAILURE

    _EXECUTION_EVENTS.clear()
    components, backend, _key = _components(tmp_path)
    _prepared, _proof, handle, lease_and_session = _issue_route(components, backend)
    lease, _write_session = lease_and_session
    context = _execution_context(backend, _write_session)
    _EXECUTION_FAILURE = failure
    try:
        with pytest.raises(type(failure)):
            components.proof_consumer_port.execute(handle, context)
        with pytest.raises((TypeError, ValueError), match="once|cleared|consumed|revoked"):
            components.proof_consumer_port.execute(handle, context)
        assert len(_EXECUTION_EVENTS) == 1
    finally:
        _EXECUTION_FAILURE = None
        _close_issuance(lease, _write_session)


def test_canonical_claim_time_and_route_digest_use_existing_ledger_domain(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_operations = importlib.import_module("offerpilot.ai.write_operations")
    original = write_operations.ledger_fingerprint
    calls: list[tuple[str, object]] = []

    def capture(key: object, domain: str, preimage: object) -> str:
        calls.append((domain, copy.deepcopy(preimage)))
        return original(key, domain, preimage)

    route_module = _route_module()
    monkeypatch.setattr(route_module, "ledger_fingerprint", capture, raising=False)
    components, backend, key = _components(tmp_path)
    prepared, _read = _prepare(components, backend)
    _proof, lease, _write = _issue(components, backend, prepared)
    route_calls = [item for item in calls if item[0] == "legacy-route-pending-identity-v1"]
    assert len(route_calls) == 1
    preimage = route_calls[0][1]
    assert preimage == {
        "schema": "legacy-route-pending-identity-v1",
        "adapter_kind": "legacy_deterministic",
        "operation_role": "primary",
        "route_source": "confirmation_resume",
        "conversation_id": 7,
        "conversation_scope_revision": 3,
        "pending_claim_identity": {
            "claim_id": OPERATION_ID,
            "claimed_at": "2026-08-25T08:09:10.123456Z",
        },
        "operation_id": OPERATION_ID,
        "tool_call_id": "legacy-call-1",
        "tool_name": ORDERED_ADAPTERS[0],
        "fingerprint_key_id": key.key_id,
        "normalized_args": json.loads(RAW_ARGS),
    }
    _close_issuance(lease, _write)
