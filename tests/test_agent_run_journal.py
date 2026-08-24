from __future__ import annotations

import ast
import hashlib
import hmac
import json
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import offerpilot.agent_runtime.events as events_module
import offerpilot.agent_runtime.journal as journal_module
from offerpilot.agent_runtime.budget import JournalBudgetExhausted, JournalDeadlineExceeded
from offerpilot.agent_runtime.events import (
    ContextManifestInput,
    JournalEventValidationError,
    _ordered_digest,
    canonical_json,
    normalize_context_identity,
    normalize_source_reference,
    pending_identity_fingerprint,
    prepare_context_snapshot,
    prepare_event,
)
from offerpilot.agent_runtime.keyring import JournalKeyDomain
from offerpilot.db import init_database
from offerpilot.agent_runtime.journal import (
    EventInput,
    NullRunRecorder,
    ResumedDisposition,
    RunRecorderFactory,
    SafeRunRecorder,
    SuspendedDisposition,
    TerminalDisposition,
)
from offerpilot.models import AgentEvent, ChatMessage, Conversation
from offerpilot.repositories.agent_runs import (
    AgentRunRepository,
    DispositionCommand,
    StartRunCommand,
    StartSegmentCommand,
)

KEY = JournalKeyDomain(
    key_id="11111111-1111-4111-8111-111111111111",
    secret=b"k" * 32,
)
SEGMENT_A = "22222222-2222-4222-8222-222222222222"
SEGMENT_B = "33333333-3333-4333-8333-333333333333"
CALL_A = "44444444-4444-4444-8444-444444444444"
CALL_B = "55555555-5555-4555-8555-555555555555"


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


def _recording_sha256(chunks: list[bytes]):
    def factory(initial: bytes = b"") -> RecordingDigest:
        digest = RecordingDigest(hashlib.sha256(), chunks)
        if initial:
            digest.update(initial)
        return digest

    return factory


def _recording_hmac_new(chunks: list[bytes]):
    def factory(key, msg=None, digestmod=None):
        digest = hmac.new(key, msg, digestmod)
        if msg is not None:
            chunks.append(bytes(msg))
        return RecordingDigest(digest, chunks)

    return factory


@pytest.mark.parametrize(
    ("context_type", "context_ref", "expected_type", "expected_entity"),
    [
        ("workspace", "private free text", "workspace", None),
        ("global", "https://private.example/path", "global", None),
        ("application", "37", "application", 37),
        ("application", "../../etc/passwd", "application", None),
        ("custom-private-type", "candidate secret", "unknown", None),
    ],
)
def test_context_identity_never_persists_arbitrary_strings(
    context_type: str,
    context_ref: str,
    expected_type: str,
    expected_entity: int | None,
) -> None:
    normalized = normalize_context_identity(
        context_type,
        context_ref,
        application_visible=lambda value: value == 37,
        key=KEY,
    )

    assert normalized.context_type == expected_type
    assert normalized.entity_id == expected_entity
    serialized = json.dumps(asdict(normalized), ensure_ascii=False)
    assert "private" not in serialized
    assert "candidate secret" not in serialized
    assert "../../" not in serialized


def test_mode_and_unknown_context_use_hmac_not_plain_reference() -> None:
    normalized = normalize_context_identity(
        "mode",
        "private-mode-instance",
        application_visible=lambda _value: False,
        key=KEY,
    )

    assert normalized.entity_id is None
    assert normalized.ref_fingerprint is not None
    assert len(normalized.ref_fingerprint) == 64
    assert normalized.ref_fingerprint != hashlib.sha256(b"private-mode-instance").hexdigest()


@pytest.mark.parametrize(
    ("source_type", "source_id", "expected"),
    [
        ("message", 42, "42"),
        ("transport_run", SEGMENT_A, SEGMENT_A),
        ("model_call", CALL_A, CALL_A),
        ("tool_call", "call_safe-01", "call_safe-01"),
        ("operation", "a" * 32, "a" * 32),
    ],
)
def test_source_reference_accepts_only_type_specific_stable_ids(
    source_type: str,
    source_id: object,
    expected: str,
) -> None:
    assert normalize_source_reference(source_type, source_id) == (source_type, expected)


@pytest.mark.parametrize(
    ("source_type", "source_id"),
    [
        ("message", "01"),
        ("transport_run", "NOT-A-UUID"),
        ("tool_call", "contains private spaces"),
        ("operation", "../private"),
        ("unknown", "private"),
    ],
)
def test_source_reference_rejects_invalid_or_free_strings(
    source_type: str,
    source_id: object,
) -> None:
    assert normalize_source_reference(source_type, source_id) == (None, None)


def _model_completed(**overrides: object):
    values = {
        "event_type": "model.completed",
        "execution_segment_id": SEGMENT_A,
        "model_step": 1,
        "model_call_id": CALL_A,
        "facts": {
            "assistant_kind": "text",
            "tool_call_count": 0,
            "finish_category": "stop",
        },
        "telemetry": {"duration_ms": 10},
    }
    values.update(overrides)
    return prepare_event(**values)  # type: ignore[arg-type]


def test_fact_digest_ignores_telemetry_but_not_stable_envelope() -> None:
    base = _model_completed()

    assert _model_completed(telemetry={"duration_ms": 90}).fact_digest == base.fact_digest
    assert _model_completed(execution_segment_id=SEGMENT_B).fact_digest != base.fact_digest
    assert _model_completed(model_step=2).fact_digest != base.fact_digest
    assert _model_completed(model_call_id=CALL_B).fact_digest != base.fact_digest
    assert _model_completed(telemetry={"duration_ms": 90}).payload_digest != base.payload_digest


def test_event_payload_is_canonical_bounded_and_rejects_unknown_or_sensitive_keys() -> None:
    prepared = _model_completed()
    assert prepared.payload_json == canonical_json(
        {
            "facts": {
                "assistant_kind": "text",
                "finish_category": "stop",
                "tool_call_count": 0,
            },
            "telemetry": {"duration_ms": 10},
        }
    )
    assert len(prepared.payload_json.encode("utf-8")) <= 4096

    with pytest.raises(JournalEventValidationError):
        _model_completed(facts={"assistant_kind": "text", "prompt": "private"})
    with pytest.raises(JournalEventValidationError):
        _model_completed(telemetry={"duration_ms": float("nan")})
    with pytest.raises(JournalEventValidationError):
        _model_completed(telemetry={"duration_ms": lambda: None})
    with pytest.raises(JournalEventValidationError):
        _model_completed(
            facts={
                "assistant_kind": "private answer copied into a legal field",
                "tool_call_count": 0,
                "finish_category": "stop",
            }
        )
    with pytest.raises(JournalEventValidationError):
        _model_completed(
            facts={
                "assistant_kind": {"nested": "private"},
                "tool_call_count": 0,
                "finish_category": "stop",
            }
        )
    with pytest.raises(JournalEventValidationError):
        _model_completed(facts={"assistant_kind": "text", "tool_call_count": 0})


def test_manifest_is_bounded_versioned_and_preserves_ordered_summaries() -> None:
    messages = tuple(range(1, 10_001))
    tools = tuple(f"tool_{index:03d}" for index in range(100))
    attachments = tuple(
        {"id": index + 1, "revision": index, "kind": "resume"} for index in range(100)
    )
    sources = tuple(
        {"id": index + 1, "revision": index, "kind": "application"}
        for index in range(100)
    )
    prepared = prepare_context_snapshot(
        logical_input={"messages": [{"role": "user", "content": "private input"}]},
        manifest=ContextManifestInput(messages, tools, attachments, sources),
        key=KEY,
    )
    manifest = json.loads(prepared.manifest_json)

    assert prepared.manifest_schema_version == 1
    assert prepared.fingerprint_key_id == KEY.key_id
    assert len(prepared.manifest_json.encode("utf-8")) < 16_384
    assert manifest["conversation"]["message_count"] == 10_000
    assert manifest["conversation"]["first_message_id"] == 1
    assert manifest["conversation"]["last_message_id"] == 10_000
    assert manifest["conversation"]["included_recent_message_ids"] == list(range(9985, 10001))
    assert manifest["tools"]["count"] == 100
    assert manifest["tools"]["included_names"] == list(tools[:32])
    assert len(manifest["attachments"]["included_refs"]) == 16
    assert len(manifest["domain_sources"]["included_refs"]) == 32
    assert "private input" not in prepared.manifest_json


def test_input_fingerprint_uses_exact_domain_formula() -> None:
    logical_input = {"tools": [], "messages": [{"role": "user", "content": "hello"}]}
    canonical = canonical_json(logical_input).encode("utf-8")
    expected = hmac.new(
        KEY.secret,
        b"offerpilot-agent-input-v1\0" + canonical,
        hashlib.sha256,
    ).hexdigest()

    prepared = prepare_context_snapshot(
        logical_input=logical_input,
        manifest=ContextManifestInput((), (), (), ()),
        key=KEY,
    )

    assert prepared.logical_input_fingerprint == expected


@pytest.mark.parametrize(
    "relative_path",
    [
        Path("src/offerpilot/agent_runtime/events.py"),
        Path("src/offerpilot/context_projector/manifest.py"),
    ],
)
def test_journal_sha256_is_initialized_before_bounded_updates(relative_path: Path) -> None:
    source = (Path(__file__).parents[1] / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(relative_path))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "sha256"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hashlib"
    ]

    assert calls
    assert all(not call.args for call in calls)


def test_event_sha_paths_update_fixed_utf8_byte_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks: list[bytes] = []
    baseline = _model_completed()
    expected_ordered = "sha256:" + hashlib.sha256(
        canonical_json(["界" * 5000]).encode("utf-8")
    ).hexdigest()
    monkeypatch.setattr(
        events_module,
        "hashlib",
        SimpleNamespace(sha256=_recording_sha256(chunks)),
    )

    assert _ordered_digest(["界" * 5000]) == expected_ordered
    assert _model_completed() == baseline
    assert chunks
    assert max(len(chunk) for chunk in chunks) <= 4096
    assert any(len(chunk) == 4096 for chunk in chunks)


def test_event_hmac_paths_update_fixed_utf8_byte_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks: list[bytes] = []
    value = "界" * 5000
    expected_fingerprint = hmac.new(
        KEY.secret,
        b"offerpilot-agent-pending-v1\0"
        + canonical_json(value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    monkeypatch.setattr(
        events_module,
        "hmac",
        SimpleNamespace(new=_recording_hmac_new(chunks)),
    )

    assert pending_identity_fingerprint(KEY, value) == expected_fingerprint
    prepared = prepare_context_snapshot(
        {"content": value},
        ContextManifestInput((), (), (), ()),
        key=KEY,
    )
    expected_logical = hmac.new(
        KEY.secret,
        b"offerpilot-agent-input-v1\0"
        + canonical_json({"content": value}).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert prepared.logical_input_fingerprint == expected_logical
    assert chunks
    assert max(len(chunk) for chunk in chunks) <= 4096
    assert any(len(chunk) == 4096 for chunk in chunks)


@pytest.mark.parametrize(
    "value",
    [
        {"bad": {1, 2}},
        {"bad": float("inf")},
        {"bad": lambda: None},
        {"bad": Path("private")},
    ],
)
def test_canonical_json_rejects_unsupported_values_without_stringifying(value: object) -> None:
    with pytest.raises(JournalEventValidationError):
        canonical_json(value)


def test_canonical_json_rejects_cycles() -> None:
    value: dict[str, object] = {}
    value["cycle"] = value

    with pytest.raises(JournalEventValidationError):
        canonical_json(value)


def test_canonical_json_rejects_oversized_leaf_before_serialization() -> None:
    with pytest.raises(JournalEventValidationError):
        canonical_json("x" * 1_048_577)


def test_uuid_normalizer_does_not_execute_str_subclass_methods() -> None:
    called = False

    class EvilStr(str):
        def replace(self, *_args: object, **_kwargs: object) -> str:
            nonlocal called
            called = True
            raise AssertionError("untrusted str subclass executed")

    assert normalize_source_reference("transport_run", EvilStr(SEGMENT_A)) == (None, None)
    assert called is False


def test_event_draft_is_immutable() -> None:
    event = _model_completed()
    changed = replace(event, execution_segment_id=SEGMENT_B)
    assert event.execution_segment_id == SEGMENT_A
    assert changed.execution_segment_id == SEGMENT_B


def test_context_captured_accepts_versioned_snapshot_key() -> None:
    snapshot_id = "66666666-6666-4666-8666-666666666666"
    event = prepare_event(
        event_type="context.captured",
        execution_segment_id=SEGMENT_A,
        facts={
            "snapshot_id": snapshot_id,
            "snapshot_key": f"model-input:{SEGMENT_A}:{CALL_A}",
            "manifest_digest": "a" * 64,
            "logical_input_fingerprint": "b" * 64,
        },
        fingerprint_key_id=KEY.key_id,
    )

    assert event.dedupe_key == f"context.captured:{snapshot_id}"


def test_sync_segment_has_no_transport_run_id() -> None:
    event = prepare_event(
        event_type="segment.started",
        execution_segment_id=SEGMENT_A,
        facts={
            "request_kind": "initial",
            "transport_mode": "sync",
            "execution_path": "model_turn",
            "transport_run_id": None,
        },
    )

    assert json.loads(event.payload_json)["facts"]["transport_run_id"] is None


def test_event_fixed_enums_reject_safe_looking_private_canary() -> None:
    with pytest.raises(JournalEventValidationError):
        _model_completed(
            facts={
                "assistant_kind": "private-canary",
                "tool_call_count": 0,
                "finish_category": "stop",
            }
        )


def test_hmac_facts_require_key_domain_id() -> None:
    with pytest.raises(JournalEventValidationError):
        prepare_event(
            event_type="model.requested",
            execution_segment_id=SEGMENT_A,
            model_step=1,
            model_call_id=CALL_A,
            facts={
                "snapshot_id": "66666666-6666-4666-8666-666666666666",
                "provider_kind": "openai_compatible",
                "model_id_fingerprint": "a" * 64,
                "supports_tools": True,
                "supports_json_schema": False,
                "stream": False,
                "tools_count": 3,
                "response_format_kind": "text",
            },
        )


def test_tool_shape_digests_use_explicit_sha256_prefix() -> None:
    event = prepare_event(
        event_type="tool.proposed",
        execution_segment_id=SEGMENT_A,
        facts={
            "tool_call_id": "call-1",
            "tool_name": "create_application",
            "tool_kind": "write",
            "args_shape_digest": "sha256:" + "a" * 64,
            "proposal_outcome": "confirmation_required",
        },
        source_ref_type="tool_call",
        source_ref_id="call-1",
    )
    assert event.event_type == "tool.proposed"
    with pytest.raises(JournalEventValidationError):
        prepare_event(
            event_type="tool.proposed",
            execution_segment_id=SEGMENT_A,
            facts={
                "tool_call_id": "call-1",
                "tool_name": "create_application",
                "tool_kind": "write",
                "args_shape_digest": "a" * 64,
                "proposal_outcome": "confirmation_required",
            },
            source_ref_type="tool_call",
            source_ref_id="call-1",
        )


def test_canonical_json_rejects_str_subclass_keys_without_comparing_them() -> None:
    called = False

    class EvilKey(str):
        def __lt__(self, _other: object) -> bool:
            nonlocal called
            called = True
            raise AssertionError("untrusted key comparison executed")

    with pytest.raises(JournalEventValidationError):
        canonical_json({EvilKey("a"): 1, EvilKey("b"): 2})
    assert called is False


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ScriptedClock:
    def __init__(self, values: list[float | BaseException]) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value

    def set_values(self, values: list[float | BaseException]) -> None:
        self.values = iter(values)


class RecordingJournalRepository:
    def __init__(self) -> None:
        self.append_calls = 0
        self.capture_calls = 0
        self.create_calls = 0
        self.start_segment_calls = 0
        self.converge_calls = 0
        self.dispositions: list[object] = []
        self.converge_kwargs: list[dict[str, object]] = []
        self.create_kwargs: list[dict[str, object]] = []
        self.start_segment_kwargs: list[dict[str, object]] = []
        self.mark_degraded_calls = 0
        self.mark_degraded_kwargs: list[dict[str, object]] = []
        self.append_failure: BaseException | None = None
        self.mark_degraded_failure: Exception | None = None
        self.create_failure: Exception | None = None
        self.start_segment_failure: BaseException | None = None
        self.waiting_run: object | None = None

    def append_event(self, _run_id: str, draft: object, **_kwargs: object) -> object:
        self.append_calls += 1
        if self.append_failure is not None:
            raise self.append_failure
        return draft

    def capture_context(
        self, _run_id: str, command: object, **_kwargs: object
    ) -> object:
        self.capture_calls += 1
        return command

    def converge_disposition(
        self, _run_id: str, command: object, **_kwargs: object
    ) -> tuple[object, ...]:
        self.converge_calls += 1
        self.dispositions.append(command)
        self.converge_kwargs.append(dict(_kwargs))
        return tuple(getattr(command, "events"))

    def mark_degraded(self, run_id: str, **_kwargs: object) -> object:
        self.mark_degraded_calls += 1
        self.mark_degraded_kwargs.append(dict(_kwargs))
        if self.mark_degraded_failure is not None:
            raise self.mark_degraded_failure
        return SimpleNamespace(id=run_id, recording_status="degraded")

    def create_run_and_initial_segment(
        self, command: object, **_kwargs: object
    ) -> object:
        self.create_calls += 1
        self.create_kwargs.append(dict(_kwargs))
        if self.create_failure is not None:
            raise self.create_failure
        return SimpleNamespace(run=SimpleNamespace(id=getattr(command, "run_id")))

    def find_waiting_run(
        self,
        _conversation_id: int,
        _tool_call_id: str,
        **_kwargs: object,
    ) -> object | None:
        return self.waiting_run

    def start_segment(self, command: object, **_kwargs: object) -> object:
        self.start_segment_calls += 1
        self.start_segment_kwargs.append(dict(_kwargs))
        if self.start_segment_failure is not None:
            raise self.start_segment_failure
        return getattr(command, "segment_started")


def _route_event() -> EventInput:
    return EventInput(
        event_type="route.selected",
        facts={"route_kind": "model", "route_reason_code": "model_default"},
    )


def _recorder(
    repository: RecordingJournalRepository,
    *,
    clock: ManualClock | None = None,
    event_preparer: object | None = None,
) -> SafeRunRecorder:
    return SafeRunRecorder(
        repository,  # type: ignore[arg-type]
        KEY,
        "77777777-7777-4777-8777-777777777777",
        SEGMENT_A,
        clock=clock or ManualClock(),
        event_preparer=event_preparer,  # type: ignore[arg-type]
    )


BOUND_RUN_ID = "77777777-7777-4777-8777-777777777777"
BOUND_CONFIRMATION_SEGMENT_ID = "88888888-8888-4888-8888-888888888888"
BOUND_CONFIRMATION_ATTEMPT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"
BOUND_TOOL_CALL_ID = "call-1"


class _BoundRepository(AgentRunRepository):
    def __init__(self, session_factory: object, *, clock: ManualClock) -> None:
        super().__init__(session_factory)  # type: ignore[arg-type]
        self.clock = clock
        self.bound_calls = 0
        self.bound_disposition_calls = 0
        self.after_bound: object | None = None
        self.after_bound_disposition: object | None = None

    def append_event_bound(self, session: object, run_id: str, draft: object) -> object:
        self.bound_calls += 1
        result = super().append_event_bound(session, run_id, draft)  # type: ignore[arg-type]
        if self.after_bound is not None:
            self.after_bound()  # type: ignore[operator]
        return result

    def converge_disposition_bound(
        self,
        session: object,
        run_id: str,
        command: DispositionCommand,
    ) -> object:
        self.bound_disposition_calls += 1
        result = super().converge_disposition_bound(  # type: ignore[arg-type]
            session, run_id, command
        )
        if self.after_bound_disposition is not None:
            self.after_bound_disposition()  # type: ignore[operator]
        return result


def _bound_start_command(conversation_id: int) -> StartRunCommand:
    run_started = prepare_event(
        event_type="run.started",
        execution_segment_id=SEGMENT_A,
        facts={
            "agent_run_id": BOUND_RUN_ID,
            "origin_kind": "user_message",
            "conversation_id": conversation_id,
            "context_type": "workspace",
            "transport_mode": "sync",
        },
    )
    segment_started = prepare_event(
        event_type="segment.started",
        execution_segment_id=SEGMENT_A,
        facts={
            "request_kind": "initial",
            "transport_mode": "sync",
            "execution_path": "model_turn",
            "transport_run_id": None,
        },
    )
    return StartRunCommand(
        run_id=BOUND_RUN_ID,
        conversation_id=conversation_id,
        input_message_id=None,
        origin_kind="user_message",
        initial_context_type="workspace",
        initial_context_entity_id=None,
        initial_context_ref_fingerprint=None,
        fingerprint_key_id=KEY.key_id,
        initial_transport_mode="sync",
        initial_route_kind="model",
        run_started=run_started,
        segment_started=segment_started,
    )


def _bound_draft(*, message_kind: str = "assistant") -> object:
    return prepare_event(
        event_type="assistant.persisted",
        execution_segment_id=SEGMENT_A,
        facts={"message_id": 991, "message_kind": message_kind},
        source_ref_type="message",
        source_ref_id=991,
    )


def _seed_bound_recorder(
    tmp_path: Path,
    clock: ManualClock,
) -> tuple[_BoundRepository, object, SafeRunRecorder, object, int]:
    session_factory = init_database(tmp_path / "bound.db")
    with session_factory() as seed:
        conversation = Conversation(title="bound transaction")
        seed.add(conversation)
        seed.flush()
        conversation_id = conversation.id
        seed.commit()
    repository = _BoundRepository(session_factory, clock=clock)
    repository.create_run_and_initial_segment(_bound_start_command(conversation_id))
    recorder = SafeRunRecorder(
        repository,
        KEY,
        BOUND_RUN_ID,
        SEGMENT_A,
        clock=clock,
    )
    return repository, session_factory, recorder, _bound_draft(), conversation_id


def _write_bound_marker(session: object, conversation_id: int) -> None:
    session.add(  # type: ignore[union-attr]
        ChatMessage(
            conversation_id=conversation_id,
            role="assistant",
            content="bound-domain-marker",
        )
    )


def _load_bound_rows(
    session_factory: object,
    draft: object,
) -> tuple[AgentEvent | None, ChatMessage | None]:
    with session_factory() as session:  # type: ignore[operator]
        event = session.scalar(  # type: ignore[union-attr]
            select(AgentEvent).where(AgentEvent.dedupe_key == draft.dedupe_key)  # type: ignore[union-attr]
        )
        marker = session.scalar(  # type: ignore[union-attr]
            select(ChatMessage).where(ChatMessage.content == "bound-domain-marker")
        )
        return event, marker


def _approval_decided_draft(*, decision: str = "approved") -> object:
    return prepare_event(
        event_type="approval.decided",
        execution_segment_id=BOUND_CONFIRMATION_SEGMENT_ID,
        facts={
            "confirmation_attempt_id": BOUND_CONFIRMATION_ATTEMPT_ID,
            "tool_call_id": BOUND_TOOL_CALL_ID,
            "decision": decision,
            "original_input_fingerprint": "c" * 64,
            "decided_input_fingerprint": "c" * 64,
        },
        source_ref_type="tool_call",
        source_ref_id=BOUND_TOOL_CALL_ID,
        fingerprint_key_id=KEY.key_id,
    )


def _seed_bound_resume_recorder(
    tmp_path: Path,
    clock: ManualClock,
) -> tuple[_BoundRepository, object, SafeRunRecorder, int]:
    repository, session_factory, initial, _, conversation_id = _seed_bound_recorder(
        tmp_path, clock
    )
    initial.suspend(_suspended_command())
    repository.start_segment(
        StartSegmentCommand(
            run_id=BOUND_RUN_ID,
            segment_started=prepare_event(
                event_type="segment.started",
                execution_segment_id=BOUND_CONFIRMATION_SEGMENT_ID,
                facts={
                    "request_kind": "confirmation",
                    "transport_mode": "sync",
                    "execution_path": "agent_resume",
                    "transport_run_id": None,
                },
            ),
        )
    )
    recorder = SafeRunRecorder(
        repository,
        KEY,
        BOUND_RUN_ID,
        BOUND_CONFIRMATION_SEGMENT_ID,
        clock=clock,
    )
    return repository, session_factory, recorder, conversation_id


def test_bound_success_commits_journal_event_and_outer_domain_marker(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    repository, session_factory, recorder, draft, conversation_id = _seed_bound_recorder(
        tmp_path, clock
    )
    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.append_prepared_event_bound(session, draft) is True
            _write_bound_marker(session, conversation_id)

    event, marker = _load_bound_rows(session_factory, draft)
    assert event is not None
    assert marker is not None
    assert repository.bound_calls == 1
    assert recorder.recording_status == "healthy"


def test_bound_conflict_rolls_back_savepoint_and_commits_outer_marker(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    repository, session_factory, recorder, draft, conversation_id = _seed_bound_recorder(
        tmp_path, clock
    )
    repository.append_event(BOUND_RUN_ID, draft)  # type: ignore[arg-type]
    conflicting = _bound_draft(message_kind="tool")

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.append_prepared_event_bound(session, conflicting) is False
            _write_bound_marker(session, conversation_id)
        assert session.is_active

    event, marker = _load_bound_rows(session_factory, conflicting)
    assert event is not None
    assert marker is not None
    assert repository.bound_calls == 1
    assert recorder.diagnostics == ["journal_tool_projection_failed"]


def test_bound_exhausted_before_entry_skips_prepare_savepoint_and_repository(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    repository, session_factory, recorder, draft, conversation_id = _seed_bound_recorder(
        tmp_path, clock
    )
    recorder.active_budget.used_seconds = recorder.active_budget.total_seconds
    prepared_calls = 0

    def should_not_prepare(_event: EventInput, _deadline: float) -> object:
        nonlocal prepared_calls
        prepared_calls += 1
        return _bound_draft()

    recorder._event_preparer = should_not_prepare  # type: ignore[assignment]
    assert recorder.prepare_event_draft(_route_event()) is None
    assert prepared_calls == 0

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.append_prepared_event_bound(session, draft) is False
            _write_bound_marker(session, conversation_id)

    event, marker = _load_bound_rows(session_factory, draft)
    assert event is None
    assert marker is not None
    assert repository.bound_calls == 0
    assert recorder.recording_status == "degraded"


def test_bound_native_overshoot_returns_true_and_preserves_outer_commit(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    repository, session_factory, recorder, draft, conversation_id = _seed_bound_recorder(
        tmp_path, clock
    )
    repository.after_bound = lambda: clock.advance(0.060)

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.append_prepared_event_bound(session, draft) is True
            _write_bound_marker(session, conversation_id)

    event, marker = _load_bound_rows(session_factory, draft)
    assert event is not None
    assert marker is not None
    assert repository.bound_calls == 1
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_budget_exhausted"]


def test_bound_ordinary_exception_rolls_back_savepoint_and_commits_outer_marker(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    repository, session_factory, recorder, draft, conversation_id = _seed_bound_recorder(
        tmp_path, clock
    )
    repository.after_bound = lambda: (_ for _ in ()).throw(RuntimeError("bound-canary"))

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.append_prepared_event_bound(session, draft) is False
            _write_bound_marker(session, conversation_id)
        assert session.is_active

    event, marker = _load_bound_rows(session_factory, draft)
    assert event is None
    assert marker is not None
    assert repository.bound_calls == 1
    assert recorder.diagnostics == ["journal_tool_projection_failed"]


@pytest.mark.parametrize("base_error", [KeyboardInterrupt, SystemExit])
def test_bound_base_exception_cleans_savepoint_and_preserves_original_priority(
    tmp_path: Path,
    base_error: type[BaseException],
) -> None:
    clock = ManualClock()
    repository, session_factory, recorder, draft, conversation_id = _seed_bound_recorder(
        tmp_path, clock
    )
    repository.after_bound = lambda: (_ for _ in ()).throw(base_error())

    with session_factory() as session:  # type: ignore[operator]
        session.begin()
        with pytest.raises(base_error):
            recorder.append_prepared_event_bound(session, draft)
        _write_bound_marker(session, conversation_id)
        session.flush()
        session.rollback()
        assert session.is_active

    event, marker = _load_bound_rows(session_factory, draft)
    assert event is None
    assert marker is None
    assert repository.bound_calls == 1
    assert recorder.recording_status == "healthy"


def test_prepared_event_draft_is_cpu_only_and_does_not_persist_degraded_state() -> None:
    repository = RecordingJournalRepository()

    def fail_prepare(_event: EventInput, _deadline: float) -> object:
        raise RuntimeError("prepare-canary")

    recorder = _recorder(repository, event_preparer=fail_prepare)

    assert recorder.prepare_event_draft(_route_event()) is None
    assert repository.append_calls == 0
    assert repository.mark_degraded_calls == 0
    assert recorder.diagnostics == ["journal_tool_projection_failed"]


def test_bound_cleanup_failure_never_syncs_degraded_through_caller_session(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    repository, session_factory, recorder, draft, conversation_id = _seed_bound_recorder(
        tmp_path, clock
    )
    sync_calls = 0

    def fail_cleanup(_lease: object) -> None:
        raise RuntimeError("cleanup-canary")

    def record_sync(_lease: object) -> None:
        nonlocal sync_calls
        sync_calls += 1

    recorder._cleanup_operation = fail_cleanup  # type: ignore[method-assign]
    recorder._sync_degraded = record_sync  # type: ignore[method-assign]
    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.append_prepared_event_bound(session, draft) is True
            _write_bound_marker(session, conversation_id)

    event, marker = _load_bound_rows(session_factory, draft)
    assert event is not None
    assert marker is not None
    assert repository.bound_calls == 1
    assert sync_calls == 0
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_cleanup_failed"]


def test_combined_bound_resume_uses_caller_session_and_preserves_event_order(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    repository, session_factory, recorder, conversation_id = (
        _seed_bound_resume_recorder(tmp_path, clock)
    )
    original_factory = repository.session_factory

    def forbid_second_session() -> object:
        raise AssertionError("bound Journal path opened a second Session")

    repository.session_factory = forbid_second_session  # type: ignore[assignment]
    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.record_approval_and_resume_bound(
                session,
                _approval_decided_draft(),  # type: ignore[arg-type]
                _resumed_command(),
            )
            assert session.in_transaction()
            _write_bound_marker(session, conversation_id)
    repository.session_factory = original_factory

    events = repository.list_events(BOUND_RUN_ID)
    event_types = [event.event_type for event in events]
    assert event_types.index("approval.decided") < event_types.index("run.resumed")
    assert event_types.count("approval.decided") == 1
    assert event_types.count("run.resumed") == 1
    assert repository.bound_calls == 1
    assert repository.bound_disposition_calls == 1
    assert repository.get_run(BOUND_RUN_ID).status == "running"  # type: ignore[union-attr]

    with session_factory() as session:  # type: ignore[operator]
        assert session.scalar(
            select(ChatMessage).where(ChatMessage.content == "bound-domain-marker")
        ) is not None


@pytest.mark.parametrize("combined", (False, True))
def test_bound_resume_requires_active_caller_transaction(
    tmp_path: Path,
    combined: bool,
) -> None:
    repository, session_factory, recorder, _ = _seed_bound_resume_recorder(
        tmp_path, ManualClock()
    )

    with session_factory() as session:  # type: ignore[operator]
        if combined:
            recorded = recorder.record_approval_and_resume_bound(
                session,
                _approval_decided_draft(),  # type: ignore[arg-type]
                _resumed_command(),
            )
        else:
            recorded = recorder.resume_bound(session, _resumed_command())
        assert recorded is False

    event_types = [event.event_type for event in repository.list_events(BOUND_RUN_ID)]
    assert "approval.decided" not in event_types
    assert "run.resumed" not in event_types
    run = repository.get_run(BOUND_RUN_ID)
    assert run is not None
    assert run.status == "waiting_confirmation"
    assert run.waiting_tool_call_id == BOUND_TOOL_CALL_ID
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_resume_failed"]


def test_combined_bound_resume_is_idempotent_on_same_recorder(tmp_path: Path) -> None:
    repository, session_factory, recorder, _ = _seed_bound_resume_recorder(
        tmp_path, ManualClock()
    )
    draft = _approval_decided_draft()

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.record_approval_and_resume_bound(
                session, draft, _resumed_command()  # type: ignore[arg-type]
            )
            assert recorder.record_approval_and_resume_bound(
                session, draft, _resumed_command()  # type: ignore[arg-type]
            )

    assert repository.bound_calls == 1
    assert repository.bound_disposition_calls == 1
    assert [
        event.event_type for event in repository.list_events(BOUND_RUN_ID)
    ].count("run.resumed") == 1


def test_combined_bound_resume_rolls_back_with_outer_transaction(tmp_path: Path) -> None:
    repository, session_factory, recorder, conversation_id = (
        _seed_bound_resume_recorder(tmp_path, ManualClock())
    )

    with session_factory() as session:  # type: ignore[operator]
        session.begin()
        _write_bound_marker(session, conversation_id)
        session.flush()
        assert recorder.record_approval_and_resume_bound(
            session,
            _approval_decided_draft(),  # type: ignore[arg-type]
            _resumed_command(),
        )
        session.rollback()

    event_types = [event.event_type for event in repository.list_events(BOUND_RUN_ID)]
    assert "approval.decided" not in event_types
    assert "run.resumed" not in event_types
    run = repository.get_run(BOUND_RUN_ID)
    assert run is not None
    assert run.status == "waiting_confirmation"
    assert run.waiting_tool_call_id == BOUND_TOOL_CALL_ID
    with session_factory() as session:  # type: ignore[operator]
        assert session.scalar(
            select(ChatMessage).where(ChatMessage.content == "bound-domain-marker")
        ) is None


def test_combined_bound_resume_replays_on_same_recorder_after_outer_rollback(
    tmp_path: Path,
) -> None:
    repository, session_factory, recorder, conversation_id = _seed_bound_resume_recorder(
        tmp_path, ManualClock()
    )
    draft = _approval_decided_draft()

    with session_factory() as session:  # type: ignore[operator]
        session.begin()
        _write_bound_marker(session, conversation_id)
        session.flush()
        assert recorder.record_approval_and_resume_bound(
            session, draft, _resumed_command()  # type: ignore[arg-type]
        )
        session.rollback()

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.record_approval_and_resume_bound(
                session, draft, _resumed_command()  # type: ignore[arg-type]
            )

    event_types = [event.event_type for event in repository.list_events(BOUND_RUN_ID)]
    assert event_types.count("approval.decided") == 1
    assert event_types.count("run.resumed") == 1
    assert repository.bound_calls == 2
    assert repository.bound_disposition_calls == 2
    assert repository.get_run(BOUND_RUN_ID).status == "running"  # type: ignore[union-attr]


def test_resume_bound_replays_on_same_recorder_after_outer_rollback(
    tmp_path: Path,
) -> None:
    repository, session_factory, recorder, conversation_id = _seed_bound_resume_recorder(
        tmp_path, ManualClock()
    )

    with session_factory() as session:  # type: ignore[operator]
        session.begin()
        _write_bound_marker(session, conversation_id)
        session.flush()
        assert recorder.resume_bound(session, _resumed_command())
        session.rollback()

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.resume_bound(session, _resumed_command())

    event_types = [event.event_type for event in repository.list_events(BOUND_RUN_ID)]
    assert event_types.count("run.resumed") == 1
    assert repository.bound_disposition_calls == 2
    assert repository.get_run(BOUND_RUN_ID).status == "running"  # type: ignore[union-attr]


def test_combined_bound_resume_replay_after_commit_remains_idempotent(
    tmp_path: Path,
) -> None:
    repository, session_factory, recorder, conversation_id = (
        _seed_bound_resume_recorder(tmp_path, ManualClock())
    )
    draft = _approval_decided_draft()

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            _write_bound_marker(session, conversation_id)
            session.flush()
            assert recorder.record_approval_and_resume_bound(
                session, draft, _resumed_command()  # type: ignore[arg-type]
            )

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.record_approval_and_resume_bound(
                session, draft, _resumed_command()  # type: ignore[arg-type]
            )

    event_types = [event.event_type for event in repository.list_events(BOUND_RUN_ID)]
    assert event_types.count("approval.decided") == 1
    assert event_types.count("run.resumed") == 1
    assert repository.bound_calls == 2
    assert repository.bound_disposition_calls == 2
    assert recorder.recording_status == "healthy"


def test_combined_bound_failure_recovers_degraded_resume_after_caller_transaction(
    tmp_path: Path,
) -> None:
    repository, session_factory, recorder, conversation_id = (
        _seed_bound_resume_recorder(tmp_path, ManualClock())
    )
    repository.after_bound = lambda: (_ for _ in ()).throw(
        RuntimeError("bound-approval-canary")
    )
    approval_draft = _approval_decided_draft()
    command = _resumed_command()

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert (
                recorder.record_approval_and_resume_bound(
                    session,
                    approval_draft,  # type: ignore[arg-type]
                    command,
                )
                is False
            )
            assert session.is_active
            _write_bound_marker(session, conversation_id)

    waiting = repository.get_run(BOUND_RUN_ID)
    assert waiting is not None and waiting.status == "waiting_confirmation"
    assert recorder.recover_approval_and_resume(
        approval_draft,  # type: ignore[arg-type]
        command,
    )
    assert recorder.recover_approval_and_resume(
        approval_draft,  # type: ignore[arg-type]
        command,
    )
    recorder.append_event(_route_event())

    event_types = [event.event_type for event in repository.list_events(BOUND_RUN_ID)]
    assert event_types.count("approval.decided") == 1
    assert event_types.count("run.resumed") == 1
    assert event_types.count("route.selected") == 1
    assert event_types.index("approval.decided") < event_types.index("run.resumed")
    run = repository.get_run(BOUND_RUN_ID)
    assert run is not None
    assert run.status == "running"
    assert run.waiting_tool_call_id is None
    assert run.recording_status == "degraded"
    assert run.recording_error_count == 1
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_resume_failed"]
    with session_factory() as session:  # type: ignore[operator]
        assert session.scalar(
            select(ChatMessage).where(ChatMessage.content == "bound-domain-marker")
        ) is not None


def test_resume_bound_ordinary_conflict_is_fail_open_and_degrades_recorder(
    tmp_path: Path,
) -> None:
    repository, session_factory, recorder, conversation_id = (
        _seed_bound_resume_recorder(tmp_path, ManualClock())
    )
    conflicting = ResumedDisposition(
        confirmation_attempt_id=BOUND_CONFIRMATION_ATTEMPT_ID,
        tool_call_id="different-call",
    )

    with session_factory() as session:  # type: ignore[operator]
        with session.begin():
            assert recorder.resume_bound(session, conflicting) is False
            assert session.is_active
            _write_bound_marker(session, conversation_id)

    assert repository.get_run(BOUND_RUN_ID).status == "waiting_confirmation"  # type: ignore[union-attr]
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_resume_failed"]


def test_segment_budget_includes_preprocessing_and_stops_nonterminal_writes() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()

    def slow_prepare(value: EventInput, _deadline: float) -> object:
        clock.advance(0.151)
        return prepare_event(
            event_type=value.event_type,
            execution_segment_id=SEGMENT_A,
            facts=dict(value.facts),
        )

    recorder = _recorder(repository, clock=clock, event_preparer=slow_prepare)
    recorder.append_event(_route_event())

    assert recorder.recording_status == "degraded"
    assert repository.append_calls == 0
    assert recorder.diagnostics == ["journal_budget_exhausted"]


def test_active_work_budget_ignores_gap_between_recorder_calls() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)

    recorder.append_event(_route_event())
    used_after_first = recorder.active_budget.used_seconds
    clock.advance(2.0)
    recorder.append_event(_route_event())

    assert recorder.recording_status == "healthy"
    assert repository.append_calls == 2
    assert recorder.active_budget.used_seconds == used_after_first


def test_five_thirty_ms_operations_share_segment_budget_and_later_work_is_noop() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    prepares = 0

    def thirty_ms_prepare(value: EventInput, _deadline: float) -> object:
        nonlocal prepares
        prepares += 1
        clock.advance(0.030)
        return prepare_event(
            event_type=value.event_type,
            execution_segment_id=SEGMENT_A,
            facts=dict(value.facts),
        )

    recorder = _recorder(repository, clock=clock, event_preparer=thirty_ms_prepare)
    for _ in range(6):
        recorder.append_event(_route_event())

    assert recorder.recording_status == "degraded"
    assert prepares == 5
    assert repository.append_calls == 4
    assert recorder.diagnostics == ["journal_budget_exhausted"]


def test_native_overshoot_keeps_successful_result_then_latches_degraded() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()

    def overshooting_capture(_run_id: str, command: object, **_kwargs: object) -> object:
        clock.advance(0.060)
        repository.capture_calls += 1
        return command

    repository.capture_context = overshooting_capture  # type: ignore[method-assign]
    recorder = _recorder(repository, clock=clock)

    snapshot_id = recorder.capture_context(
        {"messages": [1]},
        ContextManifestInput((), (), (), ()),
        snapshot_kind="model_input",
        model_step=1,
        model_call_id=CALL_A,
    )
    assert snapshot_id is not None
    assert repository.capture_calls == 1
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_budget_exhausted"]


def _segment_started(segment_id: str = SEGMENT_B) -> object:
    return prepare_event(
        event_type="segment.started",
        execution_segment_id=segment_id,
        facts={
            "request_kind": "confirmation",
            "transport_mode": "sync",
            "execution_path": "agent_resume",
            "transport_run_id": None,
        },
    )


def test_public_journal_protocol_and_builder_types_are_exported() -> None:
    assert {
        "RunRecorder",
        "StartRunBuilder",
        "StartSegmentBuilder",
    }.issubset(journal_module.__all__)
    assert journal_module.RunRecorder is not None
    assert journal_module.StartRunBuilder is not None
    assert journal_module.StartSegmentBuilder is not None


def test_base_exception_work_propagates_after_final_clock_failure_and_unlock() -> None:
    repository = RecordingJournalRepository()
    clock_values: list[float | BaseException] = [0.0, 0.0, SystemExit()]

    def clock() -> float:
        value = clock_values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def interrupt_prepare(_value: EventInput, _deadline: float) -> object:
        raise KeyboardInterrupt

    recorder = _recorder(
        repository,
        clock=clock,  # type: ignore[arg-type]
        event_preparer=interrupt_prepare,
    )

    with pytest.raises(KeyboardInterrupt):
        recorder.append_event(_route_event())

    assert recorder.active_budget.clock_invalid_latched is True
    assert recorder.active_budget.used_seconds == recorder.active_budget.total_seconds
    assert "journal_clock_invalid" in recorder.diagnostics
    assert recorder._operation_lock.acquire(blocking=False)
    recorder._operation_lock.release()


def test_cleanup_exception_releases_lock_charges_and_uses_closed_diagnostic() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)

    def fail_cleanup(_lease: object) -> None:
        raise RuntimeError("private-cleanup-canary")

    recorder._cleanup_operation = fail_cleanup  # type: ignore[method-assign]
    recorder.append_event(_route_event())

    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_cleanup_failed"]
    assert "private-cleanup-canary" not in json.dumps(recorder.diagnostics)
    assert recorder._operation_lock.acquire(blocking=False)
    recorder._operation_lock.release()


def test_cleanup_degradation_persists_with_current_non_exhausted_lease() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)

    def fail_cleanup(_lease: object) -> None:
        raise RuntimeError("private-cleanup-canary")

    recorder._cleanup_operation = fail_cleanup  # type: ignore[method-assign]
    recorder.append_event(_route_event())

    assert recorder.recording_status == "degraded"
    assert repository.mark_degraded_calls == 1
    assert repository.mark_degraded_kwargs[0]["deadline"] == pytest.approx(0.045)
    assert hasattr(repository.mark_degraded_kwargs[0]["safe_clock"], "sample")


@pytest.mark.parametrize(
    ("primary_error", "sync_error", "expected_error"),
    [
        (None, KeyboardInterrupt("sync-keyboard-canary"), KeyboardInterrupt),
        (None, SystemExit("sync-system-exit-canary"), SystemExit),
        (
            KeyboardInterrupt("primary-keyboard-canary"),
            SystemExit("sync-system-exit-canary"),
            KeyboardInterrupt,
        ),
        (
            SystemExit("primary-system-exit-canary"),
            KeyboardInterrupt("sync-keyboard-canary"),
            SystemExit,
        ),
    ],
)
def test_ordinary_cleanup_sync_base_exception_preserves_priority(
    primary_error: BaseException | None,
    sync_error: BaseException,
    expected_error: type[BaseException],
) -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)
    repository.append_failure = primary_error
    repository.mark_degraded_failure = sync_error  # type: ignore[assignment]

    def fail_cleanup(_lease: object) -> None:
        clock.advance(0.010)
        raise RuntimeError("ordinary-cleanup-canary")

    recorder._cleanup_operation = fail_cleanup  # type: ignore[method-assign]

    raised: BaseException | None = None
    try:
        recorder.append_event(_route_event())
    except BaseException as error:
        raised = error

    expected_message = str(primary_error if primary_error is not None else sync_error)
    assert raised is not None
    assert type(raised) is expected_error
    assert str(raised) == expected_message
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_cleanup_failed"]
    assert "ordinary-cleanup-canary" not in json.dumps(recorder.diagnostics)
    assert recorder.active_budget.used_seconds == pytest.approx(0.010)
    assert recorder._current_lease is None
    assert repository.mark_degraded_calls == 1
    assert recorder._operation_lock.acquire(blocking=False)
    recorder._operation_lock.release()


def test_identity_degradation_persists_with_current_non_exhausted_lease() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)

    recorder.start_segment(
        SimpleNamespace(
            run_id="88888888-8888-4888-8888-888888888888",
            segment_started=_segment_started(),
        )  # type: ignore[arg-type]
    )

    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_segment_identity_changed"]
    assert repository.mark_degraded_calls == 1
    assert repository.mark_degraded_kwargs[0]["deadline"] == pytest.approx(0.045)
    assert hasattr(repository.mark_degraded_kwargs[0]["safe_clock"], "sample")


def test_diagnostic_sink_is_reentrant_concurrent_and_deduplicated() -> None:
    repository = RecordingJournalRepository()
    repository.append_failure = RuntimeError("private-write-canary")
    recorder = _recorder(repository)
    sink_codes: list[str] = []
    sink_codes_lock = threading.Lock()
    blocked = threading.Event()

    def sink(code: str) -> None:
        with sink_codes_lock:
            sink_codes.append(code)
        probe_done = threading.Event()

        def probe_state_lock() -> None:
            with recorder._state_lock:
                probe_done.set()

        probe = threading.Thread(target=probe_state_lock)
        probe.start()
        if not probe_done.wait(timeout=0.25):
            blocked.set()
        probe.join(timeout=1.0)
        recorder._diagnose(code)

    recorder._diagnostic_sink = sink
    recorder.append_event(_route_event())

    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def degrade_concurrently() -> None:
        try:
            barrier.wait(timeout=1.0)
            recorder._degrade("journal_concurrent_sink")
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=degrade_concurrently) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=1.0)
    for thread in threads:
        thread.join(timeout=1.0)

    assert errors == []
    assert not blocked.is_set()
    assert sink_codes.count("journal_event_write_failed") == 1
    assert sink_codes.count("journal_concurrent_sink") == 1
    assert recorder.diagnostics.count("journal_event_write_failed") == 1
    assert recorder.diagnostics.count("journal_concurrent_sink") == 1


def test_concurrent_operations_serialize_and_lock_wait_hits_hard_cap() -> None:
    repository = RecordingJournalRepository()
    started = threading.Event()
    release = threading.Event()

    original_append = repository.append_event

    def blocking_append(run_id: str, draft: object, **kwargs: object) -> object:
        started.set()
        release.wait(timeout=1.0)
        return original_append(run_id, draft, **kwargs)

    repository.append_event = blocking_append  # type: ignore[method-assign]
    recorder = _recorder(repository, clock=time.monotonic)
    first = threading.Thread(target=lambda: recorder.append_event(_route_event()))
    second_done = threading.Event()

    first.start()
    assert started.wait(timeout=1.0)

    def second_call() -> None:
        recorder.append_event(_route_event())
        second_done.set()

    second = threading.Thread(target=second_call)
    second.start()
    time.sleep(0.08)
    release.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert second_done.is_set()
    assert repository.append_calls == 1
    assert recorder.recording_status == "degraded"
    assert "journal_budget_exhausted" in recorder.diagnostics


def test_serialized_operations_charge_only_their_own_post_lock_work() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)
    first_prepare = threading.Event()
    release_first = threading.Event()
    second_waiting = threading.Event()
    prepare_count = 0
    prepare_count_lock = threading.Lock()
    errors: list[BaseException] = []

    def prepare(value: EventInput, _deadline: float) -> object:
        nonlocal prepare_count
        with prepare_count_lock:
            prepare_count += 1
            number = prepare_count
        clock.advance(0.030)
        if number == 1:
            first_prepare.set()
            if not release_first.wait(timeout=1.0):
                raise AssertionError("first operation was not released")
        return prepare_event(
            event_type=value.event_type,
            execution_segment_id=SEGMENT_A,
            facts=dict(value.facts),
        )

    recorder._event_preparer = prepare  # type: ignore[assignment]
    original_acquire = recorder._acquire_operation
    acquire_count = 0
    acquire_count_lock = threading.Lock()

    def tracked_acquire(lease: object) -> bool:
        nonlocal acquire_count
        with acquire_count_lock:
            acquire_count += 1
            number = acquire_count
        if number == 2:
            second_waiting.set()
        return original_acquire(lease)  # type: ignore[arg-type]

    recorder._acquire_operation = tracked_acquire  # type: ignore[method-assign]

    def call_recorder() -> None:
        try:
            recorder.append_event(_route_event())
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=call_recorder)
    second = threading.Thread(target=call_recorder)
    first.start()
    assert first_prepare.wait(timeout=1.0)
    second.start()
    assert second_waiting.wait(timeout=1.0)
    release_first.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert errors == []
    assert repository.append_calls == 2
    assert recorder.recording_status == "healthy"
    assert recorder.diagnostics == []


def test_safe_recorder_does_not_swallow_base_exception() -> None:
    repository = RecordingJournalRepository()
    repository.append_failure = KeyboardInterrupt()
    recorder = _recorder(repository)

    with pytest.raises(KeyboardInterrupt):
        recorder.append_event(_route_event())


def test_safe_recorder_diagnostics_never_include_exception_text_and_latch() -> None:
    repository = RecordingJournalRepository()
    repository.append_failure = RuntimeError("private-user-canary")
    recorder = _recorder(repository)

    recorder.append_event(_route_event())
    recorder.append_event(_route_event())

    assert recorder.recording_status == "degraded"
    assert repository.append_calls == 1
    assert repository.mark_degraded_calls == 1
    assert "private-user-canary" not in json.dumps(recorder.diagnostics)
    assert recorder.diagnostics == ["journal_event_write_failed"]


def test_sqlite_lock_exhaustion_is_classified_as_budget_exhaustion() -> None:
    class LockedError(Exception):
        sqlite_errorcode = 5

    repository = RecordingJournalRepository()
    repository.append_failure = OperationalError(
        "private statement",
        {"private": "params"},
        LockedError("private lock"),
    )
    recorder = _recorder(repository)

    recorder.append_event(_route_event())

    assert recorder.diagnostics == ["journal_budget_exhausted"]
    assert "private" not in json.dumps(recorder.diagnostics)


def test_prelatched_clock_invalid_precedes_saturated_budget_diagnostic() -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository)
    recorder.active_budget.latch_clock_invalid()

    recorder.append_event(_route_event())

    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_clock_invalid"]
    assert repository.append_calls == 0


def test_safe_recorder_captures_context_with_model_identity() -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository)

    snapshot_id = recorder.capture_context(
        {"messages": [1]},
        ContextManifestInput(
            conversation_message_ids=(1,),
            tool_names=("get_application",),
            attachment_refs=(),
            domain_source_refs=(),
        ),
        snapshot_kind="model_input",
        model_step=1,
        model_call_id=CALL_A,
    )

    assert snapshot_id is not None
    assert repository.capture_calls == 1
    assert recorder.recording_status == "healthy"


def test_mark_degraded_failure_is_safe_and_does_not_recurse() -> None:
    repository = RecordingJournalRepository()
    repository.append_failure = RuntimeError("write canary")
    repository.mark_degraded_failure = RuntimeError("mark canary")
    recorder = _recorder(repository)

    recorder.append_event(_route_event())

    assert repository.mark_degraded_calls == 1
    assert recorder.diagnostics == [
        "journal_event_write_failed",
        "journal_mark_degraded_failed",
    ]
    assert "canary" not in json.dumps(recorder.diagnostics)


@pytest.mark.parametrize("disposition_kind", ["suspended", "terminal"])
def test_degraded_recorder_attempts_final_convergence_only_once(
    disposition_kind: str,
) -> None:
    repository = RecordingJournalRepository()
    repository.append_failure = RuntimeError("fail")
    recorder = _recorder(repository)
    recorder.append_event(_route_event())

    if disposition_kind == "suspended":
        command = SuspendedDisposition(
            tool_call_id="call-1",
            tool_name="create_application",
            tool_kind="write",
            args_shape_digest="sha256:" + "a" * 64,
            pending_identity_fingerprint="b" * 64,
        )
        recorder.suspend(command)
        recorder.suspend(command)
    else:
        command = TerminalDisposition(status="failed", failure_code="provider_error")
        recorder.finish(command)
        recorder.finish(command)

    assert repository.converge_calls == 1
    event_types = [
        event.event_type for event in getattr(repository.dispositions[0], "events")
    ]
    if disposition_kind == "suspended":
        assert event_types == [
            "tool.proposed",
            "approval.requested",
            "run.waiting_confirmation",
            "segment.finished",
        ]
    else:
        assert event_types == ["run.failed", "segment.finished"]


@pytest.mark.parametrize("disposition_kind", ["suspended", "terminal"])
def test_successful_final_disposition_persists_prior_active_degradation(
    disposition_kind: str,
) -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)
    default_prepare = recorder._prepare_event
    prepare_count = 0

    def degrade_first_prepare(value: EventInput, deadline: float) -> object:
        nonlocal prepare_count
        prepare_count += 1
        if prepare_count == 1:
            clock.advance(0.151)
        return default_prepare(value, deadline)

    recorder._event_preparer = degrade_first_prepare  # type: ignore[assignment]
    recorder.append_event(_route_event())
    assert recorder.recording_status == "degraded"
    assert repository.mark_degraded_calls == 0

    if disposition_kind == "suspended":
        recorder.suspend(
            SuspendedDisposition(
                tool_call_id="call-1",
                tool_name="create_application",
                tool_kind="write",
                args_shape_digest="sha256:" + "a" * 64,
                pending_identity_fingerprint="b" * 64,
            )
        )
    else:
        recorder.finish(TerminalDisposition(status="completed"))

    assert repository.converge_calls == 1
    assert repository.mark_degraded_calls == 1
    assert recorder.recording_status == "degraded"
    assert repository.mark_degraded_kwargs[0]["deadline"] == pytest.approx(0.196)
    assert hasattr(repository.mark_degraded_kwargs[0]["safe_clock"], "sample")


def test_factory_returns_null_recorder_when_key_is_unavailable() -> None:
    repository = RecordingJournalRepository()
    factory = RunRecorderFactory(repository, key=None)  # type: ignore[arg-type]

    recorder = factory.start_run(SimpleNamespace(run_id="unused"))  # type: ignore[arg-type]

    assert isinstance(recorder, NullRunRecorder)
    assert recorder.diagnostics == ["journal_secret_unavailable"]


def test_factory_environment_switch_returns_silent_null_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OFFERPILOT_AGENT_JOURNAL_ENABLED", "false")
    repository = RecordingJournalRepository()
    factory = RunRecorderFactory(repository, key=KEY)  # type: ignore[arg-type]

    recorder = factory.start_run(SimpleNamespace(run_id="unused"))  # type: ignore[arg-type]

    assert isinstance(recorder, NullRunRecorder)
    assert recorder.diagnostics == []


def test_factory_run_creation_failure_is_fail_open_and_safely_classified() -> None:
    repository = RecordingJournalRepository()
    repository.create_failure = RuntimeError("private-create-canary")
    factory = RunRecorderFactory(repository, key=KEY)  # type: ignore[arg-type]
    segment = prepare_event(
        event_type="segment.started",
        execution_segment_id=SEGMENT_A,
        facts={
            "request_kind": "initial",
            "transport_mode": "sync",
            "execution_path": "model_turn",
            "transport_run_id": None,
        },
    )

    recorder = factory.start_run(
        SimpleNamespace(
            run_id="77777777-7777-4777-8777-777777777777",
            fingerprint_key_id=KEY.key_id,
            segment_started=segment,
        )  # type: ignore[arg-type]
    )

    assert isinstance(recorder, NullRunRecorder)
    assert recorder.diagnostics == ["journal_run_create_failed"]
    assert "canary" not in json.dumps(recorder.diagnostics)


def test_factory_final_clock_invalid_authoritatively_replaces_generic_failure() -> None:
    repository = RecordingJournalRepository()
    repository.create_failure = RuntimeError("private-create-canary")
    clock_values: list[float | BaseException] = [0.0, 0.0, 0.0, SystemExit()]

    def clock() -> float:
        value = clock_values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    factory = RunRecorderFactory(  # type: ignore[arg-type]
        repository,
        key=KEY,
        clock=clock,  # type: ignore[arg-type]
    )
    recorder = factory.start_run(
        SimpleNamespace(
            run_id="77777777-7777-4777-8777-777777777777",
            fingerprint_key_id=KEY.key_id,
            segment_started=_segment_started(SEGMENT_A),
        )  # type: ignore[arg-type]
    )

    assert isinstance(recorder, NullRunRecorder)
    assert recorder.diagnostics == ["journal_clock_invalid"]


@pytest.mark.parametrize(
    ("failure", "clock", "expected"),
    [
        (
            OperationalError(
                "private statement",
                {"private": "params"},
                type("LockedError", (Exception,), {"sqlite_errorcode": 5})(
                    "private lock"
                ),
            ),
            ManualClock(),
            "journal_budget_exhausted",
        ),
        (JournalDeadlineExceeded("deadline"), ManualClock(), "journal_budget_exhausted"),
        (JournalDeadlineExceeded("clock_invalid"), ManualClock(), "journal_clock_invalid"),
    ],
)
def test_resume_start_segment_keeps_budget_and_clock_classifications(
    failure: BaseException,
    clock: ManualClock,
    expected: str,
) -> None:
    repository = RecordingJournalRepository()
    repository.waiting_run = SimpleNamespace(
        id="77777777-7777-4777-8777-777777777777",
        fingerprint_key_id=KEY.key_id,
    )
    repository.start_segment_failure = failure
    factory = RunRecorderFactory(  # type: ignore[arg-type]
        repository,
        key=KEY,
        clock=clock,
    )

    recorder = factory.resume_waiting_run(
        1,
        "call-1",
        SimpleNamespace(
            run_id="77777777-7777-4777-8777-777777777777",
            segment_started=_segment_started(),
        ),  # type: ignore[arg-type]
    )

    assert isinstance(recorder, NullRunRecorder)
    assert recorder.diagnostics == [expected]


def test_resume_start_segment_operational_error_final_clock_invalid_is_authoritative() -> None:
    repository = RecordingJournalRepository()
    repository.waiting_run = SimpleNamespace(
        id="77777777-7777-4777-8777-777777777777",
        fingerprint_key_id=KEY.key_id,
    )
    repository.start_segment_failure = OperationalError(
        "private statement",
        {"private": "params"},
        RuntimeError("private lock"),
    )
    clock_values: list[float | BaseException] = [0.0, 0.0, 0.0, SystemExit()]

    def clock() -> float:
        value = clock_values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    factory = RunRecorderFactory(  # type: ignore[arg-type]
        repository,
        key=KEY,
        clock=clock,  # type: ignore[arg-type]
    )
    recorder = factory.resume_waiting_run(
        1,
        "call-1",
        SimpleNamespace(
            run_id="77777777-7777-4777-8777-777777777777",
            segment_started=_segment_started(),
        ),  # type: ignore[arg-type]
    )

    assert isinstance(recorder, NullRunRecorder)
    assert recorder.diagnostics == ["journal_clock_invalid"]


def test_factory_success_returns_safe_recorder_for_initial_segment() -> None:
    repository = RecordingJournalRepository()
    clock = ManualClock()
    factory = RunRecorderFactory(
        repository,  # type: ignore[arg-type]
        key=KEY,
        clock=clock,
    )
    segment = prepare_event(
        event_type="segment.started",
        execution_segment_id=SEGMENT_A,
        facts={
            "request_kind": "initial",
            "transport_mode": "sync",
            "execution_path": "model_turn",
            "transport_run_id": None,
        },
    )

    recorder = factory.start_run(
        SimpleNamespace(
            run_id="77777777-7777-4777-8777-777777777777",
            fingerprint_key_id=KEY.key_id,
            segment_started=segment,
        )  # type: ignore[arg-type]
    )

    assert isinstance(recorder, SafeRunRecorder)
    assert recorder.run_id == "77777777-7777-4777-8777-777777777777"
    assert recorder.segment_id == SEGMENT_A
    assert repository.create_kwargs[0]["deadline"] == pytest.approx(0.045)
    assert (
        repository.create_kwargs[0]["safe_clock"]._reader
        == recorder.active_budget.safe_monotonic_read
    )


def test_factory_budget_starts_before_deferred_command_preprocessing() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    factory = RunRecorderFactory(
        repository,  # type: ignore[arg-type]
        key=KEY,
        clock=clock,
    )

    def slow_builder(_key: JournalKeyDomain, guard: object) -> object:
        del guard
        clock.advance(0.151)
        return SimpleNamespace()

    recorder = factory.start_run(slow_builder)  # type: ignore[arg-type]

    assert isinstance(recorder, NullRunRecorder)
    assert recorder.diagnostics == ["journal_budget_exhausted"]
    assert repository.create_calls == 0


def test_changed_key_domain_returns_null_recorder_without_raising() -> None:
    repository = RecordingJournalRepository()
    repository.waiting_run = SimpleNamespace(
        id="77777777-7777-4777-8777-777777777777",
        fingerprint_key_id="99999999-9999-4999-8999-999999999999",
    )
    factory = RunRecorderFactory(repository, key=KEY)  # type: ignore[arg-type]
    segment = prepare_event(
        event_type="segment.started",
        execution_segment_id=SEGMENT_B,
        facts={
            "request_kind": "confirmation",
            "transport_mode": "sync",
            "execution_path": "agent_resume",
            "transport_run_id": None,
        },
    )

    recorder = factory.resume_waiting_run(
        1,
        "call-1",
        SimpleNamespace(
            run_id="77777777-7777-4777-8777-777777777777",
            segment_started=segment,
        ),  # type: ignore[arg-type]
    )

    assert isinstance(recorder, NullRunRecorder)
    assert recorder.diagnostics == ["fingerprint_key_domain_changed"]


def test_final_disposition_preparation_uses_one_independent_fifty_ms_deadline() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    deadlines: list[float] = []

    def capture_deadline(value: EventInput, deadline: float) -> object:
        deadlines.append(deadline)
        return prepare_event(
            event_type=value.event_type,
            execution_segment_id=SEGMENT_A,
            facts=dict(value.facts),
        )

    recorder = _recorder(repository, clock=clock, event_preparer=capture_deadline)
    recorder.finish(TerminalDisposition(status="completed"))

    assert deadlines == pytest.approx([0.045, 0.045])
    assert repository.converge_calls == 1
    assert repository.converge_kwargs[0]["deadline"] == pytest.approx(0.045)
    assert hasattr(repository.converge_kwargs[0]["safe_clock"], "sample")


def _suspended_command() -> SuspendedDisposition:
    return SuspendedDisposition(
        tool_call_id="call-1",
        tool_name="create_application",
        tool_kind="write",
        args_shape_digest="sha256:" + "a" * 64,
        pending_identity_fingerprint="b" * 64,
    )


def _resumed_command() -> ResumedDisposition:
    return ResumedDisposition(
        confirmation_attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
        tool_call_id="call-1",
    )


def test_nonterminal_waiter_rechecks_state_after_final_claim_before_prepare() -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository)
    first_has_lock = threading.Event()
    release_first = threading.Event()
    second_waiting = threading.Event()
    final_waiting = threading.Event()
    allow_second = threading.Event()
    allow_final = threading.Event()
    prepared_by: list[str] = []
    prepared_lock = threading.Lock()
    original_acquire = recorder._acquire_operation
    original_append = repository.append_event

    def blocking_append(run_id: str, draft: object, **kwargs: object) -> object:
        if threading.current_thread().name == "nonterminal-a":
            first_has_lock.set()
            assert release_first.wait(timeout=1.0)
        return original_append(run_id, draft, **kwargs)

    repository.append_event = blocking_append  # type: ignore[method-assign]

    def tracked_acquire(lease: object) -> bool:
        name = threading.current_thread().name
        if name == "nonterminal-a":
            return original_acquire(lease)  # type: ignore[arg-type]
        if name == "nonterminal-b":
            second_waiting.set()
            assert allow_second.wait(timeout=1.0)
        elif name == "finalizer-c":
            final_waiting.set()
            assert allow_final.wait(timeout=1.0)
        return original_acquire(lease)  # type: ignore[arg-type]

    recorder._acquire_operation = tracked_acquire  # type: ignore[method-assign]

    def prepare(value: EventInput, deadline: float) -> object:
        del deadline
        with prepared_lock:
            prepared_by.append(threading.current_thread().name)
        return recorder._prepare_event(value, 1.0)

    recorder._event_preparer = prepare  # type: ignore[assignment]
    errors: list[BaseException] = []

    def call_nonterminal() -> None:
        try:
            recorder.append_event(_route_event())
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=call_nonterminal, name="nonterminal-a")
    second = threading.Thread(target=call_nonterminal, name="nonterminal-b")
    finalizer = threading.Thread(
        target=lambda: recorder.finish(TerminalDisposition(status="completed")),
        name="finalizer-c",
    )
    first.start()
    assert first_has_lock.wait(timeout=1.0)
    second.start()
    assert second_waiting.wait(timeout=1.0)
    finalizer.start()
    assert final_waiting.wait(timeout=1.0)
    with recorder._state_lock:
        assert recorder._disposition_state == "claimed"
    release_first.set()
    allow_second.set()
    second.join(timeout=1.0)
    assert not second.is_alive()
    allow_final.set()
    first.join(timeout=1.0)
    finalizer.join(timeout=1.0)

    assert errors == []
    assert repository.append_calls == 1
    assert repository.converge_calls == 1
    assert prepared_by.count("nonterminal-b") == 0


@pytest.mark.parametrize("finalizer_kind", ["finish", "suspend", "abandon"])
@pytest.mark.parametrize("claim_order", ["resume_first", "finalizer_first"])
@pytest.mark.parametrize("lock_order", ["resume_first", "finalizer_first"])
def test_resume_and_finalizer_event_order_is_claim_ordered(
    finalizer_kind: str,
    claim_order: str,
    lock_order: str,
) -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository)
    resume_claimed = threading.Event()
    release_resume = threading.Event()
    finalizer_started = threading.Event()
    release_finalizer = threading.Event()
    original_acquire = recorder._acquire_operation
    original_prepare = recorder._prepare_event
    event_log: list[str] = []
    errors: list[BaseException] = []

    original_append = repository.append_event

    def record_append(run_id: str, draft: object, **kwargs: object) -> object:
        event_type = getattr(draft, "event_type")
        event_log.append(event_type)
        return original_append(run_id, draft, **kwargs)

    repository.append_event = record_append  # type: ignore[method-assign]
    original_converge = repository.converge_disposition

    def record_converge(run_id: str, command: object, **kwargs: object) -> object:
        event_log.extend(event.event_type for event in getattr(command, "events"))
        return original_converge(run_id, command, **kwargs)

    repository.converge_disposition = record_converge  # type: ignore[method-assign]

    def tracked_acquire(lease: object) -> bool:
        if threading.current_thread().name == "resume" and not release_resume.is_set():
            resume_claimed.set()
            assert release_resume.wait(timeout=1.0)
        return original_acquire(lease)  # type: ignore[arg-type]

    recorder._acquire_operation = tracked_acquire  # type: ignore[method-assign]

    def prepare(value: EventInput, deadline: float) -> object:
        if (
            threading.current_thread().name == "finalizer"
            and claim_order == "finalizer_first"
        ):
            finalizer_started.set()
            assert release_finalizer.wait(timeout=1.0)
        return original_prepare(value, deadline)

    recorder._event_preparer = prepare  # type: ignore[assignment]

    def run_resume() -> None:
        try:
            recorder.resume(_resumed_command())
        except BaseException as error:
            errors.append(error)

    def run_finalizer() -> None:
        try:
            if finalizer_kind == "finish":
                recorder.finish(TerminalDisposition(status="completed"))
            elif finalizer_kind == "suspend":
                recorder.suspend(_suspended_command())
            else:
                recorder.abandon()
        except BaseException as error:
            errors.append(error)

    resume_thread = threading.Thread(target=run_resume, name="resume")
    finalizer_thread = threading.Thread(target=run_finalizer, name="finalizer")
    held_lock = lock_order == "finalizer_first"
    if held_lock:
        recorder._operation_lock.acquire()
    if claim_order == "resume_first":
        resume_thread.start()
        assert resume_claimed.wait(timeout=1.0)
        finalizer_thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with recorder._state_lock:
                if recorder._disposition_state == "claimed":
                    break
            time.sleep(0.001)
        with recorder._state_lock:
            assert recorder._disposition_state == "claimed"
        release_resume.set()
    else:
        finalizer_thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with recorder._state_lock:
                if recorder._disposition_state == "claimed":
                    break
            time.sleep(0.001)
        with recorder._state_lock:
            assert recorder._disposition_state == "claimed"
        resume_thread.start()
        release_resume.set()
    if held_lock:
        recorder._operation_lock.release()
    if claim_order == "finalizer_first":
        assert finalizer_started.wait(timeout=1.0)
        release_finalizer.set()

    resume_thread.join(timeout=1.0)
    finalizer_thread.join(timeout=1.0)
    assert errors == []

    event_types = event_log
    if claim_order == "resume_first":
        assert event_types[0] == "run.resumed"
        assert repository.converge_calls == (2 if finalizer_kind != "abandon" else 1)
    else:
        assert "run.resumed" not in event_types
        assert repository.converge_calls == (1 if finalizer_kind != "abandon" else 0)


def test_spurious_condition_wakeup_keeps_final_deadline_and_predicate() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)
    with recorder._state_lock:
        recorder._resume_state = "claimed"
    finalizer = threading.Thread(
        target=lambda: recorder.finish(TerminalDisposition(status="completed"))
    )
    finalizer.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with recorder._state_lock:
            if recorder._waits_for_resume:
                break
        time.sleep(0.001)
    with recorder._state_lock:
        assert recorder._waits_for_resume is True
        recorder._state_condition.notify_all()
    clock.advance(0.051)
    with recorder._state_lock:
        recorder._state_condition.notify_all()
    finalizer.join(timeout=1.0)

    assert not finalizer.is_alive()
    assert repository.converge_calls == 0
    assert recorder._disposition_state == "failed"
    assert recorder.diagnostics == ["journal_disposition_budget_exhausted"]


def test_final_resume_wait_timeout_consumes_disposition_right(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = RecordingJournalRepository()
    recorder = SafeRunRecorder(
        repository,  # type: ignore[arg-type]
        KEY,
        "77777777-7777-4777-8777-777777777777",
        SEGMENT_A,
        clock=lambda: 0.0,
        disposition_budget_seconds=0.001,
    )
    with recorder._state_lock:
        recorder._resume_state = "claimed"

    waits: list[float] = []

    def wait(*, timeout: float | None = None) -> bool:
        waits.append(-1.0 if timeout is None else timeout)
        if len(waits) > 1:
            raise AssertionError("Condition.wait looped after a real timeout")
        return False

    monkeypatch.setattr(recorder._state_condition, "wait", wait)
    recorder.finish(TerminalDisposition(status="completed"))

    assert waits == pytest.approx([0.001])
    assert recorder._disposition_state == "failed"
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_disposition_budget_exhausted"]
    assert repository.converge_calls == 0


def test_final_resume_wait_false_after_resume_completion_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=ManualClock())
    with recorder._state_lock:
        recorder._resume_state = "claimed"

    waits: list[float] = []

    def wait(*, timeout: float | None = None) -> bool:
        waits.append(-1.0 if timeout is None else timeout)
        with recorder._state_lock:
            recorder._resume_state = "completed"
            recorder._state_condition.notify_all()
        return False

    monkeypatch.setattr(recorder._state_condition, "wait", wait)
    recorder.finish(TerminalDisposition(status="completed"))

    assert waits == pytest.approx([0.05])
    assert recorder._disposition_state == "completed"
    assert recorder.recording_status == "healthy"
    assert recorder.diagnostics == []
    assert repository.converge_calls == 1


def test_final_resume_wait_false_with_invalid_boundary_clock_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = RecordingJournalRepository()
    clock = ScriptedClock([0.0, 0.0, SystemExit(), 0.0])
    recorder = SafeRunRecorder(
        repository,  # type: ignore[arg-type]
        KEY,
        "77777777-7777-4777-8777-777777777777",
        SEGMENT_A,
        clock=clock,
    )
    with recorder._state_lock:
        recorder._resume_state = "claimed"

    waits: list[float] = []

    def wait(*, timeout: float | None = None) -> bool:
        waits.append(-1.0 if timeout is None else timeout)
        return False

    monkeypatch.setattr(recorder._state_condition, "wait", wait)
    recorder.finish(TerminalDisposition(status="completed"))

    assert waits == pytest.approx([0.05])
    assert recorder._disposition_state == "failed"
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_clock_invalid"]
    assert repository.converge_calls == 0


def test_completed_finalizer_then_invalid_clock_is_absolute_noop() -> None:
    repository = RecordingJournalRepository()
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        if calls <= 3:
            return 0.0
        raise SystemExit

    recorder = _recorder(repository, clock=clock)  # type: ignore[arg-type]
    recorder.finish(TerminalDisposition(status="completed"))
    snapshot = (
        recorder.recording_status,
        tuple(recorder.diagnostics),
        recorder.active_budget.used_seconds,
        recorder._disposition_state,
        recorder._resume_state,
        repository.converge_calls,
        repository.append_calls,
    )
    recorder.finish(TerminalDisposition(status="failed", failure_code="ignored"))
    assert snapshot == (
        recorder.recording_status,
        tuple(recorder.diagnostics),
        recorder.active_budget.used_seconds,
        recorder._disposition_state,
        recorder._resume_state,
        repository.converge_calls,
        repository.append_calls,
    )


def test_invalid_final_entry_wins_once_and_latches_clock_diagnostic() -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=lambda: (_ for _ in ()).throw(SystemExit()))  # type: ignore[arg-type]
    recorder.finish(TerminalDisposition(status="completed"))
    assert recorder._disposition_state == "failed"
    assert recorder.diagnostics == ["journal_clock_invalid"]
    assert repository.converge_calls == 0
    recorder.finish(TerminalDisposition(status="completed"))
    assert recorder.diagnostics == ["journal_clock_invalid"]
    assert repository.converge_calls == 0


def test_final_lock_timeout_consumes_right_without_retry() -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository)
    recorder._operation_lock.acquire()
    try:
        recorder.finish(TerminalDisposition(status="completed"))
    finally:
        recorder._operation_lock.release()
    assert recorder._disposition_state == "failed"
    assert repository.converge_calls == 0
    recorder.finish(TerminalDisposition(status="completed"))
    assert recorder._disposition_state == "failed"
    assert repository.converge_calls == 0


@pytest.mark.parametrize("failure_kind", ["prepare", "cleanup"])
def test_final_base_exception_marks_state_and_unlocks_before_propagation(
    failure_kind: str,
) -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository)
    if failure_kind == "prepare":
        recorder._event_preparer = lambda _event, _deadline: (_ for _ in ()).throw(
            KeyboardInterrupt
        )  # type: ignore[assignment]
    else:
        recorder._cleanup_operation = lambda _lease: (_ for _ in ()).throw(  # type: ignore[assignment]
            KeyboardInterrupt
        )

    with pytest.raises(KeyboardInterrupt):
        recorder.finish(TerminalDisposition(status="completed"))

    assert recorder._disposition_state == "failed"
    assert recorder._operation_lock.acquire(blocking=False)
    recorder._operation_lock.release()


def test_final_cleanup_failure_syncs_degraded_with_current_lease() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)
    seen_leases: list[object] = []
    original_sync = recorder._sync_degraded

    def fail_cleanup(_lease: object) -> None:
        raise RuntimeError("final-cleanup-canary")

    def record_sync(lease: object) -> None:
        seen_leases.append(lease)
        assert recorder._current_lease is lease
        original_sync(lease)  # type: ignore[arg-type]

    recorder._cleanup_operation = fail_cleanup  # type: ignore[method-assign]
    recorder._sync_degraded = record_sync  # type: ignore[method-assign]
    recorder.finish(TerminalDisposition(status="completed"))

    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_cleanup_failed"]
    assert "final-cleanup-canary" not in json.dumps(recorder.diagnostics)
    assert recorder._disposition_state == "failed"
    assert repository.converge_calls == 1
    assert repository.mark_degraded_calls == 1
    assert len(seen_leases) == 1
    assert recorder._degraded_persisted is True
    assert repository.mark_degraded_kwargs[0]["deadline"] == pytest.approx(0.045)


@pytest.mark.parametrize(
    ("primary_error", "sync_error", "expected_error"),
    [
        (None, KeyboardInterrupt("sync-keyboard-canary"), KeyboardInterrupt),
        (None, SystemExit("sync-system-exit-canary"), SystemExit),
        (
            KeyboardInterrupt("primary-canary"),
            SystemExit("sync-system-exit-canary"),
            KeyboardInterrupt,
        ),
    ],
)
def test_final_cleanup_sync_base_exception_preserves_priority(
    primary_error: BaseException | None,
    sync_error: BaseException,
    expected_error: type[BaseException],
) -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository)

    if primary_error is not None:
        def fail_converge(_run_id: str, _command: object, **_kwargs: object) -> object:
            repository.converge_calls += 1
            raise primary_error

        repository.converge_disposition = fail_converge  # type: ignore[method-assign]

    def fail_cleanup(_lease: object) -> None:
        raise RuntimeError("final-cleanup-canary")

    recorder._cleanup_operation = fail_cleanup  # type: ignore[method-assign]
    repository.mark_degraded_failure = sync_error  # type: ignore[assignment]

    with pytest.raises(expected_error) as raised:
        recorder.finish(TerminalDisposition(status="completed"))

    expected_message = str(primary_error if primary_error is not None else sync_error)
    assert str(raised.value) == expected_message
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_cleanup_failed"]
    assert "final-cleanup-canary" not in json.dumps(recorder.diagnostics)
    assert recorder._disposition_state == "failed"
    assert repository.converge_calls == 1
    assert repository.mark_degraded_calls == 1
    assert recorder._operation_lock.acquire(blocking=False)
    recorder._operation_lock.release()


def test_degraded_abandon_persists_degraded_state_with_final_lease() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)
    recorder.recording_status = "degraded"
    recorder._degraded_persisted = False
    active_used_before = recorder.active_budget.used_seconds

    recorder.abandon()

    assert recorder._disposition_state == "completed"
    assert repository.append_calls == 1
    assert repository.mark_degraded_calls == 1
    assert recorder._degraded_persisted is True
    assert recorder.active_budget.used_seconds == active_used_before
    assert repository.mark_degraded_kwargs[0]["deadline"] == pytest.approx(0.045)


def test_stale_wait_flag_cannot_authorize_resume_after_disposition_claim() -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository)
    resume_acquire_started = threading.Event()
    release_resume = threading.Event()
    original_acquire = recorder._acquire_operation
    errors: list[BaseException] = []

    def tracked_acquire(lease: object) -> bool:
        if threading.current_thread().name == "resume":
            resume_acquire_started.set()
            assert release_resume.wait(timeout=1.0)
        return original_acquire(lease)  # type: ignore[arg-type]

    recorder._acquire_operation = tracked_acquire  # type: ignore[method-assign]

    def run_resume() -> None:
        try:
            recorder.resume(_resumed_command())
        except BaseException as error:
            errors.append(error)

    resume_thread = threading.Thread(target=run_resume, name="resume")
    resume_thread.start()
    assert resume_acquire_started.wait(timeout=1.0)
    with recorder._state_lock:
        assert recorder._resume_state == "claimed"
        recorder._disposition_state = "claimed"
        recorder._waits_for_resume = False
        recorder._wait_flag = True
    release_resume.set()
    resume_thread.join(timeout=1.0)

    assert errors == []
    assert recorder._resume_state == "failed"
    assert repository.converge_calls == 0


@pytest.mark.parametrize(
    ("final_sample", "expected_diagnostic", "expected_used", "invalid"),
    [
        (0.010, None, 0.010, False),
        (SystemExit(), "journal_clock_invalid", 0.050, True),
        (0.050, "journal_disposition_budget_exhausted", 0.050, False),
    ],
)
def test_final_budget_finishes_after_cleanup_and_preserves_segment_budget(
    monkeypatch: pytest.MonkeyPatch,
    final_sample: float | BaseException,
    expected_diagnostic: str | None,
    expected_used: float,
    invalid: bool,
) -> None:
    clock = ScriptedClock([0.0, 0.0, 0.0, 0.0, 0.0])
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)  # type: ignore[arg-type]
    active_used_before = recorder.active_budget.used_seconds
    observed: list[tuple[object, bool, float, bool]] = []
    original_finish = journal_module.ActiveWorkBudget.finish_operation

    def finish_operation(budget: object, entry: object) -> bool:
        exhausted = original_finish(budget, entry)  # type: ignore[arg-type]
        observed.append(
            (
                budget,
                exhausted,
                getattr(budget, "used_seconds"),
                getattr(budget, "clock_invalid_latched"),
            )
        )
        return exhausted

    monkeypatch.setattr(
        journal_module.ActiveWorkBudget,
        "finish_operation",
        finish_operation,
    )

    def capture(value: EventInput, _deadline: float) -> object:
        return prepare_event(
            event_type=value.event_type,
            execution_segment_id=SEGMENT_A,
            facts=dict(value.facts),
        )

    recorder._event_preparer = capture  # type: ignore[assignment]
    recorder._cleanup_operation = lambda _lease: clock.set_values([final_sample])  # type: ignore[assignment]
    recorder.finish(TerminalDisposition(status="completed"))

    assert len(observed) == 1
    final_budget, exhausted, used_seconds, clock_invalid_latched = observed[0]
    assert getattr(final_budget, "total_seconds") == pytest.approx(0.050)
    if invalid:
        assert exhausted is True
        assert clock_invalid_latched is True
    else:
        assert exhausted is (
            expected_diagnostic == "journal_disposition_budget_exhausted"
        )
        assert clock_invalid_latched is False
    assert used_seconds == pytest.approx(expected_used)
    assert recorder.active_budget.used_seconds == active_used_before
    assert recorder.diagnostics == ([] if expected_diagnostic is None else [expected_diagnostic])


def test_resume_disposition_is_atomic_and_keeps_segment_recorder_open() -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository)

    recorder.resume(
        ResumedDisposition(
            confirmation_attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
            tool_call_id="call-1",
        )
    )
    recorder.append_event(_route_event())
    recorder.finish(TerminalDisposition(status="completed"))

    assert repository.converge_calls == 2
    resumed = repository.dispositions[0]
    assert getattr(resumed, "target_status") == "running"
    assert [event.event_type for event in getattr(resumed, "events")] == ["run.resumed"]
    assert repository.append_calls == 1


def test_resumed_segment_can_be_abandoned_without_changing_run_disposition() -> None:
    repository = RecordingJournalRepository()
    recorder = _recorder(repository)

    recorder.resume(
        ResumedDisposition(
            confirmation_attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
            tool_call_id="call-1",
        )
    )
    recorder.abandon()

    assert repository.converge_calls == 1
    assert repository.append_calls == 1


def test_exhausted_initial_segment_still_converges_and_later_resumes_same_run() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    initial = _recorder(repository, clock=clock)
    clock.advance(0.151)
    initial.append_event(_route_event())

    initial.suspend(
        SuspendedDisposition(
            tool_call_id="call-1",
            tool_name="update_application_status",
            tool_kind="write",
            args_shape_digest="sha256:" + "a" * 64,
            pending_identity_fingerprint="b" * 64,
        )
    )
    resumed = SafeRunRecorder(
        repository,  # type: ignore[arg-type]
        KEY,
        initial.run_id,
        SEGMENT_B,
        clock=clock,
    )
    resumed.resume(
        ResumedDisposition(
            confirmation_attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
            tool_call_id="call-1",
        )
    )
    resumed.finish(TerminalDisposition(status="completed"))

    assert initial.diagnostics == []
    assert [getattr(command, "target_status") for command in repository.dispositions] == [
        "waiting_confirmation",
        "running",
        "completed",
    ]


def test_canonicalization_invokes_budget_guard_during_collection_traversal() -> None:
    checks = 0

    def guard() -> None:
        nonlocal checks
        checks += 1
        if checks == 8:
            raise RuntimeError("deadline")

    with pytest.raises(RuntimeError, match="deadline"):
        canonical_json(list(range(100)), budget_check=guard)
    assert checks == 8


def test_canonicalization_utf8_encoding_crosses_budget_checkpoint() -> None:
    guard = FailingGuard(fail_at=3)

    with pytest.raises(JournalBudgetExhausted):
        canonical_json("中" * 4097, budget_check=guard)

    assert guard.calls == 3


@pytest.mark.parametrize("fail_at", [11, 13])
def test_ordered_digest_crosses_encoding_and_final_digest_checkpoints(fail_at: int) -> None:
    guard = FailingGuard(fail_at=fail_at)

    with pytest.raises(JournalBudgetExhausted):
        _ordered_digest(["message"], budget_check=guard)

    assert guard.calls == fail_at


@pytest.mark.parametrize("fail_at", [14, 19])
def test_hmac_chunk_update_crosses_budget_checkpoint(fail_at: int) -> None:
    guard = FailingGuard(fail_at=fail_at)

    with pytest.raises(JournalBudgetExhausted):
        pending_identity_fingerprint(
            KEY,
            "x" * 5000,
            budget_check=guard,
        )

    assert guard.calls == fail_at


def test_context_manifest_assembly_and_final_digest_cross_checkpoints() -> None:
    logical_input = {"content": "x" * 5000}
    manifest = ContextManifestInput((), (), (), ())
    guard = FailingGuard(fail_at=199)

    with pytest.raises(JournalBudgetExhausted):
        prepare_context_snapshot(
            logical_input,
            manifest,
            key=KEY,
            budget_check=guard,
        )

    assert guard.calls == 199
