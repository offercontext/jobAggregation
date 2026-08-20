from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from offerpilot.agent_runtime.budget import JournalBudgetExhausted
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
from offerpilot.agent_runtime.journal import (
    EventInput,
    NullRunRecorder,
    ResumedDisposition,
    RunRecorderFactory,
    SafeRunRecorder,
    SuspendedDisposition,
    TerminalDisposition,
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


class RecordingJournalRepository:
    def __init__(self) -> None:
        self.append_calls = 0
        self.capture_calls = 0
        self.create_calls = 0
        self.converge_calls = 0
        self.dispositions: list[object] = []
        self.converge_kwargs: list[dict[str, object]] = []
        self.create_kwargs: list[dict[str, object]] = []
        self.mark_degraded_calls = 0
        self.append_failure: BaseException | None = None
        self.mark_degraded_failure: Exception | None = None
        self.create_failure: Exception | None = None
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
    assert repository.create_kwargs[0]["deadline"] == 0.15
    assert repository.create_kwargs[0]["clock"] is clock


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

    assert deadlines == [0.05, 0.05]
    assert repository.converge_calls == 1
    assert repository.converge_kwargs[0]["deadline"] == 0.05
    assert repository.converge_kwargs[0]["clock"] is clock


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

    assert initial.diagnostics == ["journal_budget_exhausted"]
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
