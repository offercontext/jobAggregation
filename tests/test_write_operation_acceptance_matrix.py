from __future__ import annotations

# mypy: disable-error-code="no-untyped-def,no-untyped-call"

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_authority import AuthorityFactory, AuthorityUse, TrustedContextScope
from offerpilot.ai.tool_authority.fingerprint import authorization_scope_fingerprint
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.policy_types import ToolCapability, UndoPolicy
from offerpilot.ai.tool_runtime.contracts import (
    ConfirmationRequired,
    ProviderToolContract,
    ToolExceptionMapping,
)
from offerpilot.ai.tool_runtime.metadata import (
    OperationRouteIdentityV1,
    ToolMetadataBundleV1,
    ToolPresentationBindingV1,
    UndoBuilderBinding,
)
from offerpilot.ai.tool_runtime.pipeline import prepare_call
from offerpilot.ai.types import ToolCall
from offerpilot.ai.tool_specs import legacy as legacy_specs
from offerpilot.ai.tool_runtime.legacy_proof import (
    LegacyApprovedConfirmationInput,
    LegacyConfirmationLookupIdentity,
)
from offerpilot.ai.write_operations import (
    OperationCommitted,
    OperationFailed,
    OperationReplay,
    OperationUnknown,
    PendingRouteIdentityV1,
    WriteOperationCoordinator,
    WriteOperationRepository,
    ledger_fingerprint,
    load_or_create_ledger_key,
    operation_request_fingerprint,
)
from offerpilot.db import init_database
from offerpilot.models import ChatMessage, Conversation, WriteOperation
from offerpilot.pilot_runtime.composition import (
    _SqlAlchemyLegacyPendingIdentityBackend,
    build_production_tool_metadata_components,
)
from offerpilot.pilot_runtime.contracts import LegacyExecutionContext
from offerpilot.pilot_runtime.legacy_route import (
    build_legacy_pending_identity_verifier_port,
)
from offerpilot.repositories.application_jd_versions import ApplicationJDService
from offerpilot.repositories.application_outcomes import ApplicationOutcomesRepository
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.chat import ChatRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository
from tests.tool_metadata.factories import (
    compose_synthetic_bundle,
    synthetic_tool_spec,
    write_metadata,
)
from tests.tool_metadata.golden import load_asset
from tests.tool_authority.test_pending_claim import (
    compensation_route,
    create_primary_with_typed_route,
    legacy_pending_route,
)


_OPERATION_MATRIX = load_asset("tool_operation_matrix_current.json")
_TYPED_WRITE_NAMES = tuple(
    item["name"]
    for item in _OPERATION_MATRIX["typed_operations"]
    if item["operation_kind"] == "transactional_write"
)
_UNDO_KINDS = {
    item["primary_tool"]: item["undo_payload_kind"]
    for item in _OPERATION_MATRIX["required_undo_bindings"]
}
_TEST_AUTHORITY_FACTORIES: list[AuthorityFactory] = []
_TEST_SEGMENT_LEASES: list[object] = []
_LEGACY_EXECUTION_CALLS: list[str] = []


def _acceptance_execute_jd(_encoded_args: str, _context: object) -> str:
    _LEGACY_EXECUTION_CALLS.append("save_application_jd_version")
    return '{"ok":true}'


def _acceptance_execute_snapshot(_encoded_args: str, _context: object) -> str:
    _LEGACY_EXECUTION_CALLS.append("create_application_submission_snapshot")
    return '{"ok":true}'


def _acceptance_execute_outcome(_encoded_args: str, _context: object) -> str:
    _LEGACY_EXECUTION_CALLS.append("record_application_outcome")
    return '{"ok":true}'


def _acceptance_execute_non_string(_encoded_args: str, _context: object) -> str:
    return 7  # type: ignore[return-value]


_LEGACY_ARGUMENTS = {
    "save_application_jd_version": {
        "application_id": 1,
        "jd_text": "JD",
        "expected_current_version_id": None,
        "idempotency_key": "acceptance_jd_0001",
    },
    "create_application_submission_snapshot": {
        "application_id": 1,
        "resume_id": 1,
        "jd_version_id": 1,
        "material_kit_id": None,
        "submitted_at": "2026-08-27T00:00:00+00:00",
        "note": "acceptance",
        "idempotency_key": "acceptance_snapshot_0001",
    },
    "record_application_outcome": {
        "application_id": 1,
        "submission_snapshot_id": 1,
        "application_event_id": None,
        "stage": "interview",
        "result": "advanced",
        "feedback_text": "",
        "reflection_text": "",
        "next_action_text": "",
        "feedback_tags": [],
        "occurred_at": "2026-08-27T00:00:00+00:00",
        "idempotency_key": "acceptance_outcome_0001",
    },
}


def _capture_acceptance_undo_seed(_context: object, _args: object) -> None:
    return None


def _build_acceptance_undo(_seed: object, record: object) -> dict[str, object]:
    prepared = getattr(record, "prepared")
    tool_name = getattr(getattr(prepared, "spec"), "name")
    return {"kind": _UNDO_KINDS[tool_name]}


def _render_acceptance_success(result: dict[str, object]) -> str:
    return f"committed:{result['adapter']}"


@pytest.fixture(autouse=True)
def _close_test_authority_factories():
    try:
        yield
    finally:
        while _TEST_SEGMENT_LEASES:
            getattr(_TEST_SEGMENT_LEASES.pop(), "close")()
        while _TEST_AUTHORITY_FACTORIES:
            factory = _TEST_AUTHORITY_FACTORIES.pop()
            factory.close()
            assert factory.active_count == 0


def _harness(
    tmp_path,
    tool_name: str,
    executor,
    *,
    declared_failure_categories=frozenset(),
    exception_map=(),
):
    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    conversation = chat.create_conversation("workspace")
    operation_id = str(uuid4())
    pending = PendingAction(
        tool_call_id=f"acceptance-{tool_name.replace(':', '-')}-{operation_id[:8]}",
        tool_name=tool_name,
        args="{}",
        human=f"execute {tool_name}",
        operation_id=operation_id,
    )
    arguments_digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
    pending_action_revision = _pending_revision(
        pending.tool_call_id, pending.tool_name, pending.args
    )
    proposal_fingerprint = ledger_fingerprint(key, "write-operation-proposal-v1", {})
    confirmation_token_fingerprint = ledger_fingerprint(
        key, "write-operation-confirmation-token-v1", b"synthetic-token"
    )
    with sessions() as session:
        owner = session.get(Conversation, conversation.id)
        assert owner is not None
        owner.pending_tool_call_id = pending.tool_call_id
        owner.pending_operation_id = operation_id
        owner.pending_tool_name = pending.tool_name
        owner.pending_args = pending.args
        owner.pending_human = pending.human
        create_primary_with_typed_route(
            repository,
            session,
            operation_id=operation_id,
            conversation_id=conversation.id,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            raw_args=pending.args,
            proposal_fingerprint=proposal_fingerprint,
            confirmation_token_fingerprint=confirmation_token_fingerprint,
            authorization_scope_fingerprint=authorization_scope_fingerprint(
                key,
                conversation_id=conversation.id,
                conversation_scope_revision=0,
                context_type="workspace",
                context_ref=None,
                mode="general",
                capability_profile_id="agent_typed_v1",
                capability_policy_version="capability-policy-v1",
                binding_policy_version="binding-policy-v1",
                capability_profile_fingerprint="sha256:" + "0" * 64,
                binding_policy_fingerprint="sha256:" + "0" * 64,
            ),
        )
        session.commit()

    factory = AuthorityFactory()
    _TEST_AUTHORITY_FACTORIES.append(factory)
    pending_identity = SimpleNamespace(
        conversation_id=conversation.id,
        operation_id=operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        pending_action_revision=pending_action_revision,
        effective_args_digest=arguments_digest,
    )
    factory.register_pending(pending_identity)
    authority = factory.create_approval_authority(
        operation_id=operation_id,
        conversation_id=conversation.id,
        conversation_scope_revision=0,
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        pending_identity=pending_identity,
        pending_action_revision=pending_action_revision,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        effective_args_digest=arguments_digest,
        capabilities=frozenset(ToolCapability),
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=ApplicationsRepository(sessions),
        events=ApplicationEventsRepository(sessions),
        notes=NotesRepository(sessions),
        offers=OffersRepository(sessions),
        resumes=ResumesRepository(sessions),
        jd_analyses=JDAnalysesRepository(sessions),
        run_recorder=NullRunRecorder(),
    )
    parameters = {"type": "object", "properties": {}}
    contract = ProviderToolContract(
        payload={
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "acceptance",
                "parameters": parameters,
            },
        },
        name=tool_name,
        description="acceptance",
        parameters=parameters,
    )
    undo_policy = UndoPolicy.REQUIRED if tool_name in _UNDO_KINDS else UndoPolicy.NONE
    metadata = replace(
        write_metadata(
            name=tool_name,
            undo_policy=undo_policy,
            resolver_descriptors=(),
        ),
        editable_fields=(),
    )
    base = synthetic_tool_spec(tool_name, metadata=metadata)
    undo_binding = None
    if undo_policy is UndoPolicy.REQUIRED:
        operation = metadata.operation
        assert operation.undo_builder_id is not None
        undo_binding = UndoBuilderBinding(
            descriptor=operation,
            implementation_id=operation.undo_builder_id,
            capture_seed=_capture_acceptance_undo_seed,
            build_undo=_build_acceptance_undo,
        )
    spec = replace(
        base,
        contract=contract,
        executor=executor,
        presentation=ToolPresentationBindingV1(
            implementation_id="acceptance_write_presentation_v1",
            confirmation_description=base.presentation.confirmation_description,
            pending_details_projector=base.presentation.pending_details_projector,
            success_summary_projector=_render_acceptance_success,
        ),
        undo_builder_binding=undo_binding,
        declared_failure_categories=declared_failure_categories,
        exception_map=exception_map,
        success_renderer=_render_acceptance_success,
    )
    catalog = ToolCatalog((spec,), expected_names=(tool_name,))
    source = compose_synthetic_bundle()
    bundle = ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest={**source["manifest"], "typed_tools": (tool_name,)},
        legacy_boundary=source["legacy_boundary"],
        compensation=source["compensation"],
    )
    lease = bundle.open_segment_lease()
    _TEST_SEGMENT_LEASES.append(lease)
    factory.bind_segment_tool_catalog(
        authority,
        authority_metadata_view=bundle.authority_view(),
        catalog_lease=lease,
    )
    prepare_identity = factory.create_approved_write_prepare_identity(
        authority,
        approval_context=context,
        request_identity=object(),
    )
    prepared_result = prepare_call(
        lease,
        context,
        ToolCall(pending.tool_call_id, pending.tool_name, pending.args),
        call_identity=prepare_identity,
        pending_identity=pending_identity,
        pending_action_revision=pending_action_revision,
        record_proposal=False,
    )
    assert isinstance(prepared_result, ConfirmationRequired)
    request_fingerprint = operation_request_fingerprint(
        key,
        operation_id=operation_id,
        tool_call_id=pending.tool_call_id,
        approved=True,
        edited_args_present=False,
        edited_args=None,
        rejection_feedback_present=False,
        rejection_feedback="",
        confirmation_token_fingerprint=confirmation_token_fingerprint,
        proposal_fingerprint=proposal_fingerprint,
    )
    return SimpleNamespace(
        sessions=sessions,
        repository=repository,
        chat=chat,
        context=context,
        coordinator=WriteOperationCoordinator(repository),
        conversation=conversation,
        operation_id=operation_id,
        prepared=prepared_result.prepared,
        prepare_identity=prepare_identity,
        request_fingerprint=request_fingerprint,
        factory=factory,
        lease=lease,
    )


def _legacy_harness(tmp_path):
    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    verifier = build_legacy_pending_identity_verifier_port(
        backend=_SqlAlchemyLegacyPendingIdentityBackend(),
        ledger_key=key,
    )
    components = build_production_tool_metadata_components(
        pending_identity_verifier_port=verifier,
    )
    return sessions, repository, chat, WriteOperationCoordinator(repository), components


def _legacy_confirmation_token(pending: PendingAction) -> str:
    encoded = json.dumps(
        json.loads(pending.args),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = json.dumps(
        [pending.tool_call_id, pending.tool_name, encoded],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class _AcceptanceLegacyBoundRoute:
    def __init__(
        self,
        *,
        components,
        sessions,
        write_session,
        lease,
        prepared,
        pending,
        parent_route_probe,
    ):
        self.components = components
        self.sessions = sessions
        self.write_session = write_session
        self.lease = lease
        self.prepared = prepared
        self.pending = pending
        self.parent_route_probe = parent_route_probe
        self._parent_route_handle = None
        self._projected_input = None

    def prepared_call(self):
        return self.prepared

    def prepared_input_port(self):
        return self.components.confirmation_routes.prepared_input_port

    def accept_prepared_input(self, prepared, projected):
        if prepared is not self.prepared or self._projected_input is not None:
            raise ValueError("Legacy prepared projection identity mismatch")
        self._projected_input = projected

    def execute(self, prepared=None) -> str:
        if prepared is not None and prepared is not self.prepared:
            raise ValueError("Legacy prepared projection and execution identity mismatch")
        if self._projected_input is None:
            raise ValueError("Legacy prepared input was not projected")
        routes = self.components.confirmation_routes
        locked = routes.pending_identity_verifier_port.locked_recheck(
            self.write_session,
            self.lease,
            self.prepared,
        )
        claim = routes.pending_identity_verifier_port.bind_claim(
            self.write_session,
            self.lease,
            locked,
        )
        proof = routes.proof_issuer.issue_after_claim(
            self.write_session,
            self.lease,
            claim,
            self.prepared,
        )
        proof_handle = routes.catalog.resolve_server_loaded(proof)
        assert proof_handle is not None
        digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    json.loads(self.pending.args),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        operation_identity = OperationRouteIdentityV1(
            operation_id=self.pending.operation_id,
            tool_call_id=self.pending.tool_call_id,
            revision=_pending_revision(
                self.pending.tool_call_id,
                self.pending.tool_name,
                self.pending.args,
            ),
            arguments_digest=digest,
        )
        operation_handle = self.components.operation_port.bind_legacy(
            proof_handle,
            operation_identity,
        )
        try:
            self.components.operation_port.require_legacy(
                operation_handle,
                operation_identity,
            )
            self._parent_route_handle = (
                self.components.pending_persistence_route_port.bind_primary_parent(
                    operation_handle,
                    PendingRouteIdentityV1(
                        conversation_id=self.pending.conversation_id,
                        operation_id=self.pending.operation_id,
                        tool_call_id=self.pending.tool_call_id,
                        tool_name=self.pending.tool_name,
                        pending_action_revision=operation_identity.revision,
                        pending_confirmation_claim_id=self.pending.operation_id,
                        arguments_digest=operation_identity.arguments_digest,
                    ),
                )
            )
            if self.parent_route_probe is not None:
                self.parent_route_probe.append(self._parent_route_handle)
            return routes.proof_consumer_port.execute(
                proof_handle,
                LegacyExecutionContext(
                    self.write_session,
                    ApplicationJDService(self.sessions),
                    ApplicationOutcomesRepository(self.sessions),
                ),
            )
        finally:
            self.components.operation_port.revoke_legacy(operation_handle)

    def primary_parent_route_handle(self):
        if self._parent_route_handle is None:
            raise ValueError("Legacy primary parent route is unavailable")
        return self._parent_route_handle


def _legacy_route_binder(
    *,
    sessions,
    components,
    pending: PendingAction,
    parent_route_probe: list[object] | None = None,
):
    routes = components.confirmation_routes
    lookup = LegacyConfirmationLookupIdentity(conversation_id=pending.conversation_id)
    confirmation = LegacyApprovedConfirmationInput(
        decision="approved",
        operation_id=pending.operation_id,
        confirmation_token=_legacy_confirmation_token(pending),
        edited_args_present=False,
        edited_args=None,
        rejection_feedback_present=False,
        rejection_feedback="",
    )

    @contextmanager
    def bind(write_session):
        with sessions() as read_session:
            with read_session.begin():
                prepared = routes.proof_issuer.prepare_server_loaded(
                    read_session,
                    lookup,
                    confirmation,
                )
        lease = routes.pending_identity_verifier_port.open_issuance_lease(
            write_session,
            prepared,
        )
        try:
            yield _AcceptanceLegacyBoundRoute(
                components=components,
                sessions=sessions,
                write_session=write_session,
                lease=lease,
                prepared=prepared,
                pending=pending,
                parent_route_probe=parent_route_probe,
            )
        finally:
            lease.close()

    return bind


def _pending_revision(tool_call_id: str, tool_name: str, raw_args: str) -> int:
    normalized = json.dumps(
        json.loads(raw_args),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    canonical = json.dumps(
        {"args": normalized, "tool_call_id": tool_call_id, "tool_name": tool_name},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(canonical).digest()[:8], "big") & ((1 << 63) - 1)


def _propose(chat: ChatRepository, tool_name: str, route_source: str):
    conversation = chat.create_conversation("workspace")
    operation_id = str(uuid4())
    tool_call_id = f"acceptance-{tool_name.replace(':', '-')}-{operation_id[:8]}"
    pending = PendingAction(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=json.dumps(_LEGACY_ARGUMENTS[tool_name], separators=(",", ":")),
        human=f"execute {tool_name}",
        operation_id=operation_id,
    )
    with legacy_pending_route(
        pending,
        conversation.id,
        source=route_source,
    ) as route_handle:
        assert chat.persist_pending_action(
            conversation.id,
            pending,
            [],
            route_handle=route_handle,
        )
    pending.conversation_id = conversation.id
    return conversation, pending


def _execute_typed_parent(tmp_path, tool_name: str):
    calls: list[str] = []

    def execute(_args, _context):
        calls.append(tool_name)
        return {"adapter": tool_name, "executions": len(calls)}

    harness = _harness(tmp_path, tool_name, execute)
    _consume_outer_approval_transition(harness)
    execution, _record = harness.coordinator.execute_primary(
        operation_id=harness.operation_id,
        conversation_id=harness.conversation.id,
        prepared=harness.prepared,
        context=harness.context,
        prepare_identity=harness.prepare_identity,
        request_fingerprint=harness.request_fingerprint,
        parent_route_binder=None,
    )
    assert isinstance(execution, OperationCommitted)
    return (
        harness.sessions,
        harness.repository,
        harness.chat,
        harness.context,
        harness.coordinator,
        harness.conversation,
        harness.operation_id,
        harness.prepared,
        harness.prepare_identity,
        harness.request_fingerprint,
        calls,
        execution,
    )


def _consume_outer_approval_transition(harness: SimpleNamespace) -> None:
    harness.factory.begin_prepared_execution(
        harness.prepared,
        authority=harness.context.authority,
        use=AuthorityUse.APPROVED_WRITE_PREPARE,
    )


@pytest.mark.parametrize("tool_name", _TYPED_WRITE_NAMES)
def test_all_typed_ledger_adapters_execute_once_and_replay_without_runtime_calls(
    tmp_path, monkeypatch, tool_name: str
) -> None:
    (
        _sessions,
        _repository,
        _chat,
        context,
        coordinator,
        conversation,
        operation_id,
        prepared,
        prepare_identity,
        request_fingerprint,
        calls,
        _first_execution,
    ) = _execute_typed_parent(tmp_path, tool_name)

    monkeypatch.setattr(
        "offerpilot.ai.write_operations.render_compatibility",
        lambda *_args, **_kwargs: pytest.fail("terminal replay invoked renderer"),
    )
    replay, record = coordinator.execute_primary(
        operation_id=operation_id,
        conversation_id=conversation.id,
        prepared=prepared,
        context=context,
        prepare_identity=prepare_identity,
        request_fingerprint=request_fingerprint,
        parent_route_binder=None,
    )

    assert calls == [tool_name]
    assert isinstance(replay, OperationReplay)
    assert replay.payload.status == "committed"
    assert replay.payload.result_contract == "typed_json_v1"
    assert record is None


@pytest.mark.parametrize(
    ("tool_name", "route_source"),
    (
        ("save_application_jd_version", "jd_deterministic_action"),
        ("create_application_submission_snapshot", "submission_snapshot_action"),
        ("record_application_outcome", "outcome_recording_action"),
    ),
)
def test_all_legacy_ledger_adapters_execute_once_and_replay(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    route_source: str,
) -> None:
    monkeypatch.setattr(legacy_specs, "_execute_static_jd", _acceptance_execute_jd)
    monkeypatch.setattr(
        legacy_specs,
        "_execute_static_snapshot",
        _acceptance_execute_snapshot,
    )
    monkeypatch.setattr(
        legacy_specs,
        "_execute_static_outcome",
        _acceptance_execute_outcome,
    )
    _LEGACY_EXECUTION_CALLS.clear()
    sessions, repository, chat, coordinator, components = _legacy_harness(tmp_path)
    conversation, pending = _propose(chat, tool_name, route_source)
    operation = repository.get(pending.operation_id)
    assert operation is not None
    token_fingerprint = ledger_fingerprint(
        repository.key,
        "write-operation-confirmation-token-v1",
        _legacy_confirmation_token(pending).encode("ascii"),
    )

    arguments = dict(
        operation_id=pending.operation_id,
        conversation_id=conversation.id,
        tool_call_id=pending.tool_call_id,
        tool_name=tool_name,
        request_fingerprint=operation_request_fingerprint(
            repository.key,
            operation_id=pending.operation_id,
            tool_call_id=pending.tool_call_id,
            approved=True,
            edited_args_present=False,
            edited_args=None,
            rejection_feedback_present=False,
            rejection_feedback="",
            confirmation_token_fingerprint=token_fingerprint,
            proposal_fingerprint=operation.proposal_fingerprint,
        ),
        route_binder=_legacy_route_binder(
            sessions=sessions,
            components=components,
            pending=pending,
        ),
    )
    first = coordinator.execute_legacy(**arguments)
    replay = coordinator.execute_legacy(**arguments)

    assert isinstance(first, OperationCommitted), getattr(first, "payload", first)
    assert _LEGACY_EXECUTION_CALLS == [tool_name]
    assert isinstance(replay, OperationReplay)
    assert replay.payload.result_contract == "legacy_string_v1"


def test_legacy_projection_failure_revokes_untransferred_parent_route(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_specs, "_execute_static_jd", _acceptance_execute_non_string)
    sessions, repository, chat, coordinator, components = _legacy_harness(tmp_path)
    conversation, pending = _propose(
        chat,
        "save_application_jd_version",
        "jd_clarification",
    )
    operation = repository.get(pending.operation_id)
    assert operation is not None
    parent_routes: list[object] = []
    result = coordinator.execute_legacy(
        operation_id=pending.operation_id,
        conversation_id=conversation.id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        request_fingerprint=operation_request_fingerprint(
            repository.key,
            operation_id=pending.operation_id,
            tool_call_id=pending.tool_call_id,
            approved=True,
            edited_args_present=False,
            edited_args=None,
            rejection_feedback_present=False,
            rejection_feedback="",
            confirmation_token_fingerprint=ledger_fingerprint(
                repository.key,
                "write-operation-confirmation-token-v1",
                _legacy_confirmation_token(pending).encode("ascii"),
            ),
            proposal_fingerprint=operation.proposal_fingerprint,
        ),
        route_binder=_legacy_route_binder(
            sessions=sessions,
            components=components,
            pending=pending,
            parent_route_probe=parent_routes,
        ),
    )

    assert isinstance(result, OperationUnknown)
    assert result.code == "operation_projection_failed"
    assert len(parent_routes) == 1
    assert id(parent_routes[0]) not in components.pending_persistence_route_port._records  # noqa: SLF001


def test_legacy_commit_unknown_reconciliation_revokes_parent_route(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_specs, "_execute_static_jd", _acceptance_execute_jd)
    sessions, repository, chat, coordinator, components = _legacy_harness(tmp_path)
    conversation, pending = _propose(
        chat,
        "save_application_jd_version",
        "jd_clarification",
    )
    operation = repository.get(pending.operation_id)
    assert operation is not None
    parent_routes: list[object] = []
    real_commit = Session.commit
    injected = False

    def commit_then_lose_response(session):
        nonlocal injected
        terminal = any(
            isinstance(item, WriteOperation)
            and item.adapter_kind == "legacy_deterministic"
            and item.status == "committed"
            for item in session.identity_map.values()
        )
        real_commit(session)
        if terminal and not injected:
            injected = True
            raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

    monkeypatch.setattr(Session, "commit", commit_then_lose_response)
    result = coordinator.execute_legacy(
        operation_id=pending.operation_id,
        conversation_id=conversation.id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        request_fingerprint=operation_request_fingerprint(
            repository.key,
            operation_id=pending.operation_id,
            tool_call_id=pending.tool_call_id,
            approved=True,
            edited_args_present=False,
            edited_args=None,
            rejection_feedback_present=False,
            rejection_feedback="",
            confirmation_token_fingerprint=ledger_fingerprint(
                repository.key,
                "write-operation-confirmation-token-v1",
                _legacy_confirmation_token(pending).encode("ascii"),
            ),
            proposal_fingerprint=operation.proposal_fingerprint,
        ),
        route_binder=_legacy_route_binder(
            sessions=sessions,
            components=components,
            pending=pending,
            parent_route_probe=parent_routes,
        ),
    )

    assert isinstance(result, OperationReplay)
    assert result.payload.status == "committed"
    assert len(parent_routes) == 1
    assert id(parent_routes[0]) not in components.pending_persistence_route_port._records  # noqa: SLF001


@pytest.mark.parametrize(
    ("parent_tool", "compensation_kind"),
    (
        ("update_application_status", "undo:update_application_status"),
        ("create_application", "undo:create_application"),
        ("create_application_event", "undo:create_application_event"),
        ("add_note", "undo:add_note"),
        ("create_offer", "undo:create_offer"),
    ),
)
def test_all_compensation_adapters_execute_once_and_replay(
    tmp_path, parent_tool: str, compensation_kind: str
) -> None:
    (
        _sessions,
        _repository,
        _chat,
        _context,
        coordinator,
        conversation,
        parent_operation_id,
        _prepared_call,
        _prepare_identity_value,
        _request_fingerprint,
        _parent_calls,
        _parent_execution,
    ) = _execute_typed_parent(tmp_path, parent_tool)
    calls: list[str] = []

    def compensate(_session, undo):
        calls.append(str(undo["kind"]))
        return f"compensated:{compensation_kind}"

    with compensation_route(
        coordinator.repository,
        parent_operation_id,
        compensation_kind,
    ) as (parent, operation_port, route_handle, handler_handle):
        arguments = dict(
            parent=parent,
            conversation_id=conversation.id,
            operation_port=operation_port,
            route_handle=route_handle,
            handler_handle=handler_handle,
            executor=compensate,
        )
        first = coordinator.execute_compensation(**arguments)
        replay = coordinator.execute_compensation(**arguments)

    assert len(calls) == 1
    assert isinstance(first, OperationCommitted)
    assert isinstance(replay, OperationReplay)
    assert replay.payload.result_contract == "compensation_json_v1"


def test_expired_takeover_fences_late_owner_and_detects_message_and_manifest_tamper(
    tmp_path,
) -> None:
    (
        sessions,
        repository,
        _chat,
        _context,
        _coordinator,
        _conversation,
        operation_id,
        _prepared_call,
        _prepare_identity_value,
        request_fingerprint,
        _calls,
        first_execution,
    ) = _execute_typed_parent(tmp_path, "delete_note")
    original = repository.get(operation_id)
    assert original is not None
    old_owner = first_execution.ownership
    assert old_owner is not None
    with sessions() as session:
        operation = session.get(WriteOperation, operation_id)
        assert operation is not None
        operation.delivery_lease_expires_at = 0
        session.commit()

    takeover = repository.converge_expired_delivery(operation_id)
    assert isinstance(takeover, OperationReplay)
    assert takeover.delivery_generation == 2
    with sessions() as session:
        assert repository.complete_delivery(session, old_owner, outcome="final_response") is False
        session.rollback()

    operation = repository.get(operation_id)
    assert operation is not None
    replay = repository.replay(operation, request_fingerprint)
    assert replay.final_message == "操作已提交，但后续说明生成失败。"

    with (
        sessions() as session,
        pytest.raises(IntegrityError, match="operation delivery message is immutable"),
    ):
        message = session.query(ChatMessage).filter_by(operation_id=operation_id).first()
        assert message is not None
        message.content += "tampered"
        session.commit()

    with (
        sessions() as session,
        pytest.raises(IntegrityError, match="write operation delivery is immutable"),
    ):
        operation = session.get(WriteOperation, operation_id)
        assert operation is not None
        operation.delivery_manifest_sha256 = "sha256:" + "0" * 64
        session.commit()

    stable = repository.get(operation_id)
    assert stable is not None
    assert repository.replay(stable, request_fingerprint).final_message == replay.final_message


def test_two_connections_choose_one_primary_executor_winner(tmp_path) -> None:
    calls: list[str] = []
    call_lock = Lock()

    def synchronized_executor(_args, _context):
        with call_lock:
            calls.append("delete_note")
            return {"adapter": "delete_note", "executions": len(calls)}

    harness = _harness(tmp_path, "delete_note", synchronized_executor)
    _consume_outer_approval_transition(harness)

    def approve():
        return harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
            parent_route_binder=None,
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in (pool.submit(approve), pool.submit(approve))]

    assert calls == ["delete_note"]
    assert sum(isinstance(item, OperationCommitted) for item in outcomes) == 1
    assert sum(isinstance(item, OperationReplay) for item in outcomes) == 1


def test_primary_commit_unknown_reconciles_without_second_executor_call(
    tmp_path, monkeypatch
) -> None:
    calls: list[str] = []

    def execute(_args, _context):
        calls.append("delete_note")
        return {"adapter": "delete_note", "executions": len(calls)}

    harness = _harness(tmp_path, "delete_note", execute)
    _consume_outer_approval_transition(harness)
    real_commit = Session.commit
    injected = False

    def commit_then_lose_response(session):
        nonlocal injected
        terminal = any(
            isinstance(item, WriteOperation) and item.status == "committed"
            for item in session.identity_map.values()
        )
        real_commit(session)
        if terminal and not injected:
            injected = True
            raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

    monkeypatch.setattr(Session, "commit", commit_then_lose_response)
    arguments = dict(
        operation_id=harness.operation_id,
        conversation_id=harness.conversation.id,
        prepared=harness.prepared,
        context=harness.context,
        prepare_identity=harness.prepare_identity,
        request_fingerprint=harness.request_fingerprint,
        parent_route_binder=None,
    )
    reconciled, _ = harness.coordinator.execute_primary(**arguments)
    replay, _ = harness.coordinator.execute_primary(**arguments)

    assert isinstance(reconciled, OperationReplay)
    assert isinstance(replay, OperationReplay)
    assert calls == ["delete_note"]


def test_deterministic_failure_commit_unknown_replays_failed_terminal(
    tmp_path, monkeypatch
) -> None:
    calls = 0

    def fail(_args, _context):
        nonlocal calls
        calls += 1
        raise ValueError("conflict")

    harness = _harness(
        tmp_path,
        "delete_note",
        fail,
        declared_failure_categories=frozenset({"conflict"}),
        exception_map=(ToolExceptionMapping(ValueError, "conflict", "domain_conflict"),),
    )
    _consume_outer_approval_transition(harness)
    real_commit = Session.commit
    injected = False

    def commit_then_lose_response(session):
        nonlocal injected
        failed_terminal = any(
            isinstance(item, WriteOperation) and item.status == "failed"
            for item in session.identity_map.values()
        )
        real_commit(session)
        if failed_terminal and not injected:
            injected = True
            raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

    monkeypatch.setattr(Session, "commit", commit_then_lose_response)
    arguments = dict(
        operation_id=harness.operation_id,
        conversation_id=harness.conversation.id,
        prepared=harness.prepared,
        context=harness.context,
        prepare_identity=harness.prepare_identity,
        request_fingerprint=harness.request_fingerprint,
        parent_route_binder=None,
    )
    reconciled, reconciled_record = harness.coordinator.execute_primary(**arguments)
    replay, replay_record = harness.coordinator.execute_primary(**arguments)

    assert isinstance(reconciled, OperationReplay)
    assert reconciled.payload.status == "failed"
    assert reconciled.payload.failure_code == "domain_conflict"
    assert reconciled_record is None
    assert isinstance(replay, OperationReplay)
    assert replay_record is None
    assert calls == 1


def test_commit_unknown_reconciliation_distinguishes_primary_and_compensation_states(
    tmp_path, monkeypatch
) -> None:
    harness = _harness(
        tmp_path,
        "delete_note",
        lambda _args, _context: {"adapter": "delete_note"},
    )
    repository = harness.repository
    coordinator = harness.coordinator
    proposed_id = harness.operation_id
    fingerprint = "hmac-sha256:" + "8" * 64

    primary_proposed = coordinator._reconcile_commit_unknown(
        proposed_id,
        fingerprint,
        absent_code="operation_result_unknown",
        proposed_code="operation_not_committed",
    )
    primary_absent = coordinator._reconcile_commit_unknown(
        str(uuid4()),
        fingerprint,
        absent_code="operation_result_unknown",
        proposed_code="operation_not_committed",
    )
    compensation_proposal_absent = coordinator._reconcile_commit_unknown(
        str(uuid4()),
        fingerprint,
        absent_code="operation_not_committed",
        proposed_code="operation_not_committed",
    )
    compensation_execution_absent = coordinator._reconcile_commit_unknown(
        str(uuid4()),
        fingerprint,
        absent_code="operation_result_unknown",
        proposed_code="operation_not_committed",
    )

    assert isinstance(primary_proposed, OperationUnknown)
    assert primary_proposed.code == "operation_not_committed"
    assert isinstance(primary_absent, OperationUnknown)
    assert primary_absent.code == "operation_result_unknown"
    assert isinstance(compensation_proposal_absent, OperationUnknown)
    assert compensation_proposal_absent.code == "operation_not_committed"
    assert isinstance(compensation_execution_absent, OperationUnknown)
    assert compensation_execution_absent.code == "operation_result_unknown"

    def unreadable_session():
        raise OperationalError("SELECT", {}, RuntimeError("unreadable"))

    monkeypatch.setattr(repository, "session_factory", unreadable_session)
    unreadable = coordinator._reconcile_commit_unknown(
        proposed_id,
        fingerprint,
        absent_code="operation_busy",
        proposed_code="operation_busy",
    )
    assert isinstance(unreadable, OperationUnknown)
    assert unreadable.code == "operation_result_unknown"


def test_compensation_commit_unknown_and_parent_conflict_are_stable(tmp_path, monkeypatch) -> None:
    (
        _sessions,
        _repository,
        _chat,
        _context,
        coordinator,
        conversation,
        parent_operation_id,
        _prepared_call,
        _prepare_identity_value,
        _request_fingerprint,
        _parent_calls,
        _parent_execution,
    ) = _execute_typed_parent(tmp_path, "add_note")
    calls: list[str] = []
    real_commit = Session.commit
    injected = False

    def commit_then_lose_response(session):
        nonlocal injected
        terminal_compensation = any(
            isinstance(item, WriteOperation)
            and item.operation_role == "compensation"
            and item.status == "committed"
            for item in session.identity_map.values()
        )
        real_commit(session)
        if terminal_compensation and not injected:
            injected = True
            raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

    monkeypatch.setattr(Session, "commit", commit_then_lose_response)

    def compensate(_session, _undo):
        calls.append("delete_note")
        return "compensated"

    with compensation_route(
        coordinator.repository,
        parent_operation_id,
        "undo:add_note",
    ) as (parent, operation_port, route_handle, handler_handle):
        arguments = dict(
            parent=parent,
            conversation_id=conversation.id,
            operation_port=operation_port,
            route_handle=route_handle,
            handler_handle=handler_handle,
            executor=compensate,
        )
        reconciled = coordinator.execute_compensation(**arguments)
        replay = coordinator.execute_compensation(**arguments)

    assert isinstance(reconciled, OperationReplay)
    assert isinstance(replay, OperationReplay)
    assert calls == ["delete_note"]

    conflict_parent = _execute_typed_parent(tmp_path / "conflict", "add_note")
    conflict_coordinator = conflict_parent[4]
    conflict_conversation = conflict_parent[5]
    conflict_operation_id = conflict_parent[6]
    conflict_calls = 0

    def conflict(_session, _undo):
        nonlocal conflict_calls
        conflict_calls += 1
        raise ValueError("parent_conflict")

    with compensation_route(
        conflict_coordinator.repository,
        conflict_operation_id,
        "undo:add_note",
    ) as (parent, operation_port, route_handle, handler_handle):
        conflict_arguments = dict(
            parent=parent,
            conversation_id=conflict_conversation.id,
            operation_port=operation_port,
            route_handle=route_handle,
            handler_handle=handler_handle,
            executor=conflict,
        )
        failed = conflict_coordinator.execute_compensation(**conflict_arguments)
        failed_replay = conflict_coordinator.execute_compensation(**conflict_arguments)
    assert isinstance(failed, OperationFailed)
    assert failed.payload.failure_code == "parent_conflict"
    assert isinstance(failed_replay, OperationReplay)
    assert conflict_calls == 1


def test_two_expired_takeover_connections_converge_to_one_generation(tmp_path) -> None:
    (
        sessions,
        repository,
        _chat,
        _context,
        _coordinator,
        _conversation,
        operation_id,
        _prepared_call,
        _prepare_identity_value,
        _request_fingerprint,
        _calls,
        _first_execution,
    ) = _execute_typed_parent(tmp_path, "delete_note")
    with sessions() as session:
        operation = session.get(WriteOperation, operation_id)
        assert operation is not None
        operation.delivery_lease_expires_at = 0
        session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result()
            for future in (
                pool.submit(repository.converge_expired_delivery, operation_id),
                pool.submit(repository.converge_expired_delivery, operation_id),
            )
        ]

    assert all(isinstance(item, OperationReplay) for item in outcomes)
    assert {item.delivery_generation for item in outcomes if isinstance(item, OperationReplay)} == {
        2
    }
    with sessions() as session:
        messages = session.query(ChatMessage).filter_by(operation_id=operation_id).all()
    assert [(item.delivery_kind, item.delivery_ordinal) for item in messages] == [
        ("origin_tool_result", 0),
        ("continuation_message", 1),
    ]
