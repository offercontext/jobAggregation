from __future__ import annotations

import importlib.util
import hashlib
import json
from dataclasses import FrozenInstanceError, is_dataclass
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from offerpilot.ai.interview_preparation_proposals import safe_empty_interview_preparation_proposal
from offerpilot.ai.types import Assistant
from offerpilot.db import init_database
from offerpilot.models import (
    Application,
    ApplicationEvent,
    ApplicationJDVersion,
    InterviewNote,
    InterviewPreparationProposal,
    InterviewReadinessSignal,
    KnowledgeEvidence,
    KnowledgeNoteEvidence,
    KnowledgeNoteVersion,
    Resume,
)
from offerpilot.repositories.interview_knowledge_capture import InterviewKnowledgeCaptureRepository
from offerpilot.repositories.json_contract import canonical_json, sha256_text
from offerpilot.repositories.interview_preparation_proposals import (
    InterviewPreparationConflictError,
    InterviewPreparationNotFound,
    InterviewPreparationProviderError,
    InterviewPreparationProposalsRepository,
    InterviewPreparationValidationError,
    _InterviewPreparationLeaseHeartbeat,
)
from tests.review_readiness_support import seed_review_candidate
from tests.test_review_readiness_projection import _commit_signal


JD_TEXT = "Build reliable APIs with Python."


def test_preparation_readiness_selection_loader_module_is_owner_local_asset() -> None:
    assert importlib.util.find_spec(
        "offerpilot.review_readiness.preparation_selection"
    ) is not None


class ManualClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


class ControlledWaiter:
    def __init__(self) -> None:
        self.entered = Event()
        self._wake = Event()

    def __call__(self, timeout: float) -> bool:
        self.entered.set()
        released = self._wake.wait(timeout)
        self._wake.clear()
        return released

    def release_tick(self) -> None:
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()


class CountingWaiter(ControlledWaiter):
    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0
        self.second_call = Event()

    def __call__(self, timeout: float) -> bool:
        self.call_count += 1
        if self.call_count == 2:
            self.second_call.set()
        return super().__call__(timeout)


class FailingHeartbeatSessionFactory:
    def __init__(self, factory, *, fail_calls: int = 1) -> None:  # type: ignore[no-untyped-def]
        self.factory = factory
        self.fail_calls = fail_calls
        self.calls = 0
        self.failed = Event()
        self.succeeded = Event()

    def __call__(self):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls <= self.fail_calls:
            self.failed.set()
            raise SQLAlchemyError("injected heartbeat lock failure")
        session = self.factory()
        original_commit = session.commit

        def commit(*args, **kwargs):  # type: ignore[no-untyped-def]
            result = original_commit(*args, **kwargs)
            self.succeeded.set()
            return result

        session.commit = commit  # type: ignore[method-assign]
        return session


class DeferredFailingHeartbeatSessionFactory:
    def __init__(self, factory, *, fail_calls: int) -> None:  # type: ignore[no-untyped-def]
        self._factory = factory
        self._failing = FailingHeartbeatSessionFactory(factory, fail_calls=fail_calls)
        self.enabled = False

    @property
    def calls(self) -> int:
        return self._failing.calls

    @property
    def failed(self) -> Event:
        return self._failing.failed

    @property
    def succeeded(self) -> Event:
        return self._failing.succeeded

    def __call__(self):  # type: ignore[no-untyped-def]
        if not self.enabled:
            return self._factory()
        return self._failing()


class CommitObservingSessionFactory:
    def __init__(self, factory) -> None:  # type: ignore[no-untyped-def]
        self.factory = factory
        self.enabled = False
        self.committed = Event()

    def __call__(self):  # type: ignore[no-untyped-def]
        session = self.factory()
        original_commit = session.commit

        def commit(*args, **kwargs):  # type: ignore[no-untyped-def]
            result = original_commit(*args, **kwargs)
            if self.enabled:
                self.committed.set()
            return result

        session.commit = commit  # type: ignore[method-assign]
        return session


class TrackingSession:
    def __init__(self, session, factory) -> None:  # type: ignore[no-untyped-def]
        self._session = session
        self._factory = factory

    def __enter__(self):  # type: ignore[no-untyped-def]
        self._session.__enter__()
        self._factory.active += 1
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        try:
            return self._session.__exit__(*args)
        finally:
            self._factory.active -= 1

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._session, name)


class TrackingSessionFactory:
    def __init__(self, factory) -> None:  # type: ignore[no-untyped-def]
        self.factory = factory
        self.active = 0

    def __call__(self):  # type: ignore[no-untyped-def]
        return TrackingSession(self.factory(), self)


def _as_aware_for_test(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _setup(tmp_path):
    factory = init_database(tmp_path / "data.db")
    with factory() as session:
        application = Application(company_name="Acme", position_name="Backend", source="test")
        session.add(application)
        session.flush()
        event = ApplicationEvent(
            application_id=application.id,
            event_type="interview",
            subtype="technical",
            round=2,
            scheduled_at=datetime(2026, 7, 20, 10, tzinfo=timezone.utc),
            duration_minutes=45,
            status="todo",
        )
        resume = Resume(
            title="Backend Resume",
            name="Backend Resume",
            parse_status="text-ready",
            content_json=json.dumps(
                {"experience": [{"highlights": ["Built reliable API services"]}]},
                ensure_ascii=False,
            ),
        )
        session.add_all([event, resume])
        session.commit()
        ids = (application.id, event.id, resume.id)
    return factory, ids


class SafeEmptyModel:
    supports_json_schema = False

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        return Assistant(
            content=json.dumps(safe_empty_interview_preparation_proposal(), ensure_ascii=False)
        )


class DistinctPreparationModel(SafeEmptyModel):
    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        payload = {
            "preparation_directions": [
                {
                    "id": f"direction-{self.label}",
                    "text": f"Prepare the {self.label} direction.",
                    "evidence_refs": [
                        {
                            "source": "jd",
                            "path": "/jd/text",
                            "excerpt": "Build reliable APIs with Python.",
                        }
                    ],
                }
            ],
            "story_prompts": [],
            "review_points": [],
            "interviewer_questions": [],
            "items_to_clarify": [],
        }
        return Assistant(content=json.dumps(payload, ensure_ascii=False))


class SessionClosedModel(SafeEmptyModel):
    def __init__(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self.session_factory = session_factory

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        assert self.session_factory.active == 0
        return super().complete(messages, tools)


class FailingModel:
    supports_json_schema = False

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise TimeoutError("provider is unavailable")


class BlockingSafeEmptyModel(SafeEmptyModel):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self._lock = Lock()

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        with self._lock:
            self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        return Assistant(
            content=json.dumps(safe_empty_interview_preparation_proposal(), ensure_ascii=False)
        )


class BlockingDistinctPreparationModel(DistinctPreparationModel):
    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.entered = Event()
        self.release = Event()

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.entered.set()
        assert self.release.wait(5)
        return super().complete(messages, tools)


class EventDeletingModel(SafeEmptyModel):
    def __init__(self, factory, event_id: int) -> None:
        super().__init__()
        self.factory = factory
        self.event_id = event_id

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        with self.factory() as session:
            session.execute(text("DELETE FROM application_events WHERE id=:id"), {"id": self.event_id})
            session.commit()
        return Assistant(
            content=json.dumps(safe_empty_interview_preparation_proposal(), ensure_ascii=False)
        )


def _generate(repository, ids, key, model, *, jd_text=JD_TEXT, jd_version_id=None):
    application_id, event_id, resume_id = ids
    return repository.create_generated(
        application_id=application_id,
        event_id=event_id,
        resume_id=resume_id,
        jd_text=jd_text,
        knowledge_selections=[],
        user_assertions=["I led the migration."],
        idempotency_key=key,
        model=model,
        jd_version_id=jd_version_id,
    )


def test_interview_preparation_snapshot_keeps_frozen_jd_identity(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    with factory() as session:
        version = ApplicationJDVersion(
            application_id=ids[0],
            version_number=3,
            jd_text=JD_TEXT,
            content_sha256="jd-content-sha256",
            source_kind="ui",
            idempotency_key="jd-version-test-0001",
            request_fingerprint_sha256="jd-request-sha256",
        )
        session.add(version)
        session.commit()
        version_id = version.id

    result = _generate(
        InterviewPreparationProposalsRepository(factory),
        ids,
        "snapshot-identity-01",
        SafeEmptyModel(),
        jd_version_id=version_id,
    )

    snapshot = json.loads(result.proposal.input_snapshot_json)
    assert snapshot["jd"] == {
        "text": JD_TEXT,
        "version_id": version_id,
        "version_number": 3,
        "content_sha256": "jd-content-sha256",
    }
    assert snapshot["jd_version_id"] == version_id


def test_repository_uses_injected_utc_clock_and_production_lease_defaults(tmp_path) -> None:
    factory, _ = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))

    production_repository = InterviewPreparationProposalsRepository(factory)
    repository = InterviewPreparationProposalsRepository(
        factory,
        now_factory=clock.now,
    )

    assert production_repository._lease_seconds == 30
    assert production_repository._heartbeat_interval_seconds == 10
    assert repository._lease_seconds == 30
    assert repository._heartbeat_interval_seconds == 10
    assert repository._now_factory() == clock.current


def test_heartbeat_renew_once_extends_lease_with_fenced_owner(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    token = "heartbeat-owner-token"
    with factory() as session:
        row = InterviewPreparationProposal(
            application_id=ids[0],
            application_event_id=ids[1],
            resume_id=ids[2],
            idempotency_key="heartbeat-key-0001",
            attempt_status="generating",
            generation_revision=1,
            provider_call_token=token,
            provider_lease_until=(clock.now() + timedelta(seconds=30)).replace(tzinfo=None),
            input_snapshot_json="{}",
            source_fingerprint="source-fingerprint",
        )
        session.add(row)
        session.commit()
        attempt_id = row.id

    heartbeat = _InterviewPreparationLeaseHeartbeat(
        session_factory=factory,
        attempt_id=attempt_id,
        owner_revision=1,
        owner_token=token,
        lease_seconds=30,
        now_factory=clock.now,
    )
    clock.advance(seconds=31)

    assert heartbeat.renew_once() is True
    assert heartbeat.heartbeat_count == 1
    assert heartbeat.confirmed_ownership_lost is False
    assert heartbeat.heartbeat_uncertain is False
    with factory() as session:
        row = session.get(InterviewPreparationProposal, attempt_id)
        assert row is not None
        assert row.provider_lease_until == (clock.now() + timedelta(seconds=30)).replace(
            tzinfo=None
            )


def test_heartbeat_retries_one_transient_lock_and_renews_once(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    failing_factory = FailingHeartbeatSessionFactory(factory, fail_calls=1)
    token = "heartbeat-retry-token"
    with factory() as session:
        row = InterviewPreparationProposal(
            application_id=ids[0],
            application_event_id=ids[1],
            resume_id=ids[2],
            idempotency_key="heartbeat-retry-key",
            attempt_status="generating",
            generation_revision=1,
            provider_call_token=token,
            provider_lease_until=(clock.now() + timedelta(seconds=30)).replace(
                tzinfo=None
            ),
            input_snapshot_json="{}",
            source_fingerprint="source-fingerprint",
        )
        session.add(row)
        session.commit()

    heartbeat = _InterviewPreparationLeaseHeartbeat(
        session_factory=failing_factory,
        attempt_id=row.id,
        owner_revision=1,
        owner_token=token,
        lease_seconds=30,
        now_factory=clock.now,
    )

    assert heartbeat.renew_once() is True
    assert failing_factory.calls == 2
    assert heartbeat.heartbeat_count == 1
    assert heartbeat.heartbeat_uncertain is False
    assert heartbeat.confirmed_ownership_lost is False


def test_heartbeat_uncertain_stops_future_renewals(
    tmp_path,
) -> None:
    factory, ids = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    failing_factory = FailingHeartbeatSessionFactory(factory, fail_calls=2)
    waiter = CountingWaiter()
    with factory() as session:
        row = InterviewPreparationProposal(
            application_id=ids[0],
            application_event_id=ids[1],
            resume_id=ids[2],
            idempotency_key="heartbeat-uncertain-key",
            attempt_status="generating",
            generation_revision=1,
            provider_call_token="heartbeat-uncertain-token",
            provider_lease_until=(clock.now() + timedelta(seconds=30)).replace(
                tzinfo=None
            ),
            input_snapshot_json="{}",
            source_fingerprint="source-fingerprint",
        )
        session.add(row)
        session.commit()

    heartbeat = _InterviewPreparationLeaseHeartbeat(
        session_factory=failing_factory,
        attempt_id=row.id,
        owner_revision=1,
        owner_token="heartbeat-uncertain-token",
        lease_seconds=30,
        now_factory=clock.now,
        waiter=waiter,
    )
    heartbeat.start()
    assert waiter.entered.wait(1)
    waiter.release_tick()
    assert failing_factory.failed.wait(1)
    assert heartbeat.heartbeat_uncertain is True

    waiter.release_tick()
    assert waiter.second_call.wait(0.1) is False
    heartbeat.stop_and_join()
    assert failing_factory.calls == 2


def test_heartbeat_uncertain_still_completes_final_fencing_cas(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    heartbeat_factory = DeferredFailingHeartbeatSessionFactory(factory, fail_calls=2)
    waiter = ControlledWaiter()
    repository = InterviewPreparationProposalsRepository(
        heartbeat_factory,
        lease_seconds=1,
        heartbeat_interval_seconds=10,
        now_factory=clock.now,
        waiter=waiter,
    )
    model = BlockingSafeEmptyModel()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_generate, repository, ids, "uncertain-final-cas-key", model)
        assert model.entered.wait(5)
        assert waiter.entered.wait(5)
        heartbeat_factory.enabled = True
        waiter.release_tick()
        assert heartbeat_factory.failed.wait(1)
        model.release.set()
        result = future.result(timeout=5)

    assert result.created is True
    assert result.pending is False
    assert result.attempt_status == "ready"
    assert model.calls == 1
    assert heartbeat_factory.calls == 3
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "ready"
        assert row.proposal_status == "safe_empty"
        assert row.proposal_json == canonical_json(safe_empty_interview_preparation_proposal())


def test_slow_provider_retries_transient_heartbeat_lock_and_calls_provider_once(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    heartbeat_factory = DeferredFailingHeartbeatSessionFactory(factory, fail_calls=1)
    waiter = ControlledWaiter()
    repository = InterviewPreparationProposalsRepository(
        heartbeat_factory,
        lease_seconds=1,
        heartbeat_interval_seconds=10,
        now_factory=clock.now,
        waiter=waiter,
    )
    model = BlockingSafeEmptyModel()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_generate, repository, ids, "heartbeat-e2e-key-0001", model)
        assert model.entered.wait(5)
        assert waiter.entered.wait(5)
        heartbeat_factory.enabled = True
        waiter.release_tick()
        assert heartbeat_factory.failed.wait(1)
        model.release.set()
        result = future.result(timeout=5)

    assert result.attempt_status == "ready"
    assert result.pending is False
    assert model.calls == 1
    assert heartbeat_factory.calls == 3
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "ready"
        assert row.proposal_status == "safe_empty"
    factory.kw["bind"].dispose()


def test_renewed_lease_blocks_same_key_replay_without_second_provider_call(tmp_path) -> None:
    factory_a, ids = _setup(tmp_path)
    factory_b = init_database(tmp_path / "data.db")
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    heartbeat_factory = DeferredFailingHeartbeatSessionFactory(factory_a, fail_calls=1)
    waiter = ControlledWaiter()
    repository_a = InterviewPreparationProposalsRepository(
        heartbeat_factory,
        lease_seconds=1,
        heartbeat_interval_seconds=10,
        now_factory=clock.now,
        waiter=waiter,
    )
    repository_b = InterviewPreparationProposalsRepository(
        factory_b,
        lease_seconds=1,
        now_factory=clock.now,
    )
    first_model = BlockingSafeEmptyModel()
    replay_model = SafeEmptyModel()
    key = "heartbeat-replay-key-01"

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(_generate, repository_a, ids, key, first_model)
        assert first_model.entered.wait(5)
        assert waiter.entered.wait(5)

        heartbeat_factory.enabled = True
        clock.advance(seconds=2)
        waiter.release_tick()
        assert heartbeat_factory.failed.wait(1)
        assert heartbeat_factory.succeeded.wait(1)

        with factory_a() as session:
            row = session.scalar(select(InterviewPreparationProposal))
            assert row is not None
            renewed_lease = _as_aware_for_test(row.provider_lease_until)
            assert renewed_lease is not None
            assert renewed_lease > clock.now()
            assert row.attempt_status == "generating"
            assert row.provider_call_token

        replay = _generate(repository_b, ids, key, replay_model)
        assert replay.pending is True
        assert replay.attempt_status == "generating"
        assert replay_model.calls == 0

        first_model.release.set()
        first = first_future.result(timeout=5)

    assert first.pending is False
    assert first.attempt_status == "ready"
    assert first_model.calls == 1
    assert heartbeat_factory.calls == 3
    with factory_a() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "ready"
        assert row.generation_revision == 1
        assert row.provider_call_token == ""
        assert row.provider_lease_until is None
    factory_a.kw["bind"].dispose()
    factory_b.kw["bind"].dispose()


def test_slow_provider_renews_expired_lease_and_calls_provider_once(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    waiter = ControlledWaiter()
    observed_factory = CommitObservingSessionFactory(factory)
    repository = InterviewPreparationProposalsRepository(
        observed_factory,
        lease_seconds=1,
        heartbeat_interval_seconds=10,
        now_factory=clock.now,
        waiter=waiter,
    )
    model = BlockingSafeEmptyModel()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_generate, repository, ids, "slow-heartbeat-key", model)
        assert model.entered.wait(5)
        assert waiter.entered.wait(5)
        observed_factory.enabled = True
        observed_factory.committed.clear()
        clock.advance(seconds=30)
        waiter.release_tick()
        assert observed_factory.committed.wait(1)

        with factory() as session:
            row = session.scalar(select(InterviewPreparationProposal))
            lease_until = _as_aware_for_test(row.provider_lease_until) if row else None
        assert lease_until is not None and lease_until > clock.now()

        model.release.set()
        result = future.result(timeout=5)

    assert result.created is True
    assert result.pending is False
    assert result.attempt_status == "ready"
    assert model.calls == 1


def test_expired_lease_without_takeover_can_persist_valid_provider_result(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    repository = InterviewPreparationProposalsRepository(factory, now_factory=clock.now)
    with pytest.raises(InterviewPreparationProviderError):
        _generate(repository, ids, "expired-final-key-01", FailingModel())

    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        snapshot = json.loads(row.input_snapshot_json)
        source_fingerprint = row.source_fingerprint
        row.attempt_status = "generating"
        row.generation_revision = 2
        row.provider_call_token = "expired-final-owner"
        row.provider_lease_until = (clock.now() - timedelta(seconds=1)).replace(tzinfo=None)
        session.commit()

    result = repository._call_and_store(
        model=SafeEmptyModel(),
        owner_revision=2,
        owner_token="expired-final-owner",
        application_id=ids[0],
        event_id=ids[1],
        resume_id=ids[2],
        jd_text=JD_TEXT,
        knowledge_selections=[],
        user_assertions=["I led the migration."],
        idempotency_key="expired-final-key-01",
        source_fingerprint=source_fingerprint,
        snapshot=snapshot,
        on_diagnostic=None,
    )

    assert result.pending is False
    assert result.attempt_status == "ready"


def test_heartbeat_confirms_ownership_loss_after_token_changes(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    token = "old-heartbeat-token"
    with factory() as session:
        row = InterviewPreparationProposal(
            application_id=ids[0],
            application_event_id=ids[1],
            resume_id=ids[2],
            idempotency_key="heartbeat-loss-key",
            attempt_status="generating",
            generation_revision=1,
            provider_call_token=token,
            provider_lease_until=(clock.now() + timedelta(seconds=30)).replace(tzinfo=None),
            input_snapshot_json="{}",
            source_fingerprint="source-fingerprint",
        )
        session.add(row)
        session.commit()
        attempt_id = row.id
        row.provider_call_token = "new-owner-token"
        session.commit()

    heartbeat = _InterviewPreparationLeaseHeartbeat(
        session_factory=factory,
        attempt_id=attempt_id,
        owner_revision=1,
        owner_token=token,
        lease_seconds=30,
        now_factory=clock.now,
    )

    assert heartbeat.renew_once() is False
    assert heartbeat.confirmed_ownership_lost is True
    assert heartbeat.heartbeat_uncertain is False


class _FailingHeartbeatSession:
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_args):  # type: ignore[no-untyped-def]
        return False

    def execute(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        from sqlalchemy.exc import OperationalError

        raise OperationalError("locked", {}, RuntimeError("locked"))


class _FailingHeartbeatFactory:
    def __call__(self):  # type: ignore[no-untyped-def]
        return _FailingHeartbeatSession()


def test_heartbeat_lock_failure_is_uncertain_not_confirmed_loss(tmp_path) -> None:
    _setup(tmp_path)
    heartbeat = _InterviewPreparationLeaseHeartbeat(
        session_factory=_FailingHeartbeatFactory(),
        attempt_id=1,
        owner_revision=1,
        owner_token="heartbeat-token",
        lease_seconds=30,
        now_factory=lambda: datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc),
    )

    assert heartbeat.renew_once() is False
    assert heartbeat.confirmed_ownership_lost is False
    assert heartbeat.heartbeat_uncertain is True


def test_provider_error_stops_heartbeat_worker_in_cleanup(monkeypatch, tmp_path) -> None:
    instances = []

    class TrackingHeartbeat:
        confirmed_ownership_lost = False
        heartbeat_uncertain = False

        def __init__(self, **_kwargs):
            self.started = False
            self.stopped = False
            instances.append(self)

        def start(self) -> None:
            self.started = True

        def stop_and_join(self) -> None:
            self.stopped = True

    monkeypatch.setattr(
        "offerpilot.repositories.interview_preparation_proposals._InterviewPreparationLeaseHeartbeat",
        TrackingHeartbeat,
    )
    factory, ids = _setup(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)

    with pytest.raises(InterviewPreparationProviderError):
        _generate(repository, ids, "heartbeat-cleanup-key", FailingModel())

    assert len(instances) == 1
    assert instances[0].started is True
    assert instances[0].stopped is True


def test_first_request_without_old_row_creates_lease_before_provider_and_calls_once(tmp_path) -> None:
    factory_a, ids = _setup(tmp_path)
    factory_b = init_database(tmp_path / "data.db")
    repository_a = InterviewPreparationProposalsRepository(factory_a)
    repository_b = InterviewPreparationProposalsRepository(factory_b)
    model = BlockingSafeEmptyModel()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(_generate, repository_a, ids, "first-key-00000001", model)
        assert model.entered.wait(5)
        second_future = pool.submit(_generate, repository_b, ids, "first-key-00000001", model)
        second = second_future.result(timeout=5)
        assert second.pending is True
        assert second.attempt_status == "generating"
        model.release.set()
        first = first_future.result(timeout=5)

    assert first.created is True
    assert first.proposal is not None
    assert model.calls == 1
    with factory_a() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "ready"
        assert row.generation_revision == 1
        assert row.provider_call_token == ""
    factory_a.kw["bind"].dispose()
    factory_b.kw["bind"].dispose()


def test_provider_unknown_cas_preserves_token_and_unexpired_lease(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    repository = InterviewPreparationProposalsRepository(factory, now_factory=clock.now)
    model = FailingModel()

    with pytest.raises(InterviewPreparationProviderError):
        _generate(repository, ids, "unknown-key-0000001", model)

    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "provider_unknown"
        assert row.provider_call_token
        assert row.provider_lease_until is not None
        assert row.provider_lease_until.replace(tzinfo=timezone.utc) > clock.now()
        original_token = row.provider_call_token

    retry = _generate(repository, ids, "unknown-key-0000001", SafeEmptyModel())
    assert retry.pending is True
    assert retry.attempt_status == "provider_unknown"
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.provider_call_token == original_token
    factory.kw["bind"].dispose()


def test_source_deletion_during_provider_call_invalidates_attempt_without_ready_result(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)

    with pytest.raises(InterviewPreparationConflictError) as exc_info:
        _generate(repository, ids, "event-drift-key-0001", EventDeletingModel(factory, ids[1]))

    assert exc_info.value.code == "interview_preparation_source_conflict"
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "invalidated"
        assert row.proposal_json == ""
    factory.kw["bind"].dispose()

def test_expired_lease_takeover_atomically_bumps_revision_and_only_one_wins(tmp_path) -> None:
    factory_a, ids = _setup(tmp_path)
    factory_b = init_database(tmp_path / "data.db")
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    repository_a = InterviewPreparationProposalsRepository(factory_a, now_factory=clock.now)
    repository_b = InterviewPreparationProposalsRepository(factory_b, now_factory=clock.now)
    with pytest.raises(InterviewPreparationProviderError):
        _generate(repository_a, ids, "expired-key-0000001", FailingModel())
    with factory_a() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        old_token = row.provider_call_token
        row.provider_lease_until = (clock.now() - timedelta(seconds=1)).replace(tzinfo=None)
        session.commit()

    model = BlockingSafeEmptyModel()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(_generate, repository_a, ids, "expired-key-0000001", model)
        assert model.entered.wait(5)
        second_future = pool.submit(_generate, repository_b, ids, "expired-key-0000001", model)
        second = second_future.result(timeout=5)
        model.release.set()
        first = first_future.result(timeout=5)

    assert sorted([first.pending, second.pending]) == [False, True]
    assert model.calls == 1
    with factory_a() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "ready"
        assert row.generation_revision == 2
        assert row.provider_call_token == ""
        assert old_token != row.provider_call_token
    factory_a.kw["bind"].dispose()
    factory_b.kw["bind"].dispose()


def test_takeover_closes_database_session_before_provider_call(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    seed_repository = InterviewPreparationProposalsRepository(factory, now_factory=clock.now)
    with pytest.raises(InterviewPreparationProviderError):
        _generate(seed_repository, ids, "session-close-key-0001", FailingModel())

    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        row.provider_lease_until = (clock.now() - timedelta(seconds=1)).replace(tzinfo=None)
        session.commit()

    tracking_factory = TrackingSessionFactory(factory)
    repository = InterviewPreparationProposalsRepository(
        tracking_factory,
        now_factory=clock.now,
    )
    result = _generate(repository, ids, "session-close-key-0001", SessionClosedModel(tracking_factory))

    assert result.attempt_status == "ready"
    assert result.pending is False
    assert tracking_factory.active == 0
    factory.kw["bind"].dispose()


def test_late_old_provider_result_cannot_overwrite_new_owner_ready_result(tmp_path) -> None:
    factory_a, ids = _setup(tmp_path)
    factory_b = init_database(tmp_path / "data.db")
    clock = ManualClock(datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))
    repository_a = InterviewPreparationProposalsRepository(factory_a, now_factory=clock.now)
    repository_b = InterviewPreparationProposalsRepository(factory_b, now_factory=clock.now)
    with pytest.raises(InterviewPreparationProviderError):
        _generate(repository_a, ids, "late-takeover-key-01", FailingModel())

    with factory_a() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        snapshot = json.loads(row.input_snapshot_json)
        source_fingerprint = row.source_fingerprint
        row.attempt_status = "generating"
        row.generation_revision = 1
        row.provider_call_token = "old-owner-token"
        row.provider_lease_until = (clock.now() - timedelta(seconds=1)).replace(tzinfo=None)
        session.commit()

    old_model = BlockingDistinctPreparationModel("old")
    with ThreadPoolExecutor(max_workers=2) as pool:
        old_future = pool.submit(
            repository_a._call_and_store,
            model=old_model,
            owner_revision=1,
            owner_token="old-owner-token",
            application_id=ids[0],
            event_id=ids[1],
            resume_id=ids[2],
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=["I led the migration."],
            idempotency_key="late-takeover-key-01",
            source_fingerprint=source_fingerprint,
            snapshot=snapshot,
            on_diagnostic=None,
        )
        assert old_model.entered.wait(5)
        new_result = _generate(
            repository_b,
            ids,
            "late-takeover-key-01",
            DistinctPreparationModel("new"),
        )
        old_model.release.set()
        old_result = old_future.result(timeout=5)

    assert new_result.attempt_status == "ready"
    assert old_result.attempt_status == "ready"
    with factory_a() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "ready"
        assert row.generation_revision == 2
        assert row.proposal_hash == new_result.proposal.proposal_hash  # type: ignore[union-attr]
        assert row.proposal_json == new_result.proposal.proposal_json  # type: ignore[union-attr]
        assert '"direction-new"' in row.proposal_json
        assert '"direction-old"' not in row.proposal_json
        assert row.provider_call_token == ""
        assert row.provider_lease_until is None
    factory_a.kw["bind"].dispose()
    factory_b.kw["bind"].dispose()


def test_ready_different_snapshot_returns_409_and_original_ready_remains_stable(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)
    first = _generate(repository, ids, "stable-key-0000001", SafeEmptyModel())

    with pytest.raises(InterviewPreparationConflictError) as exc_info:
        _generate(repository, ids, "stable-key-0000001", SafeEmptyModel(), jd_text="Different JD")

    assert exc_info.value.code == "interview_preparation_idempotency_conflict"
    replay = _generate(repository, ids, "stable-key-0000001", SafeEmptyModel())
    assert replay.pending is False
    assert replay.proposal is not None
    assert replay.proposal.id == first.proposal.id  # type: ignore[union-attr]
    factory.kw["bind"].dispose()


def test_same_knowledge_set_in_different_order_reuses_idempotent_proposal(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    with factory() as session:
        note = InterviewNote(
            application_id=ids[0],
            application_event_id=ids[1],
            company="Acme",
            position="Backend",
            questions="Describe the migration.",
            self_reflection="I explained the rollback plan.",
            difficulty_points="Clarify the signal.",
            mood="steady",
        )
        session.add(note)
        session.commit()
        note_id = note.id

    capture = InterviewKnowledgeCaptureRepository(factory)
    selected = [
        {"fragment_id": "questions", "path": "/questions", "start": 0, "end": 23, "text": "Describe the migration."},
        {"fragment_id": "reflection", "path": "/self_reflection", "start": 0, "end": 30, "text": "I explained the rollback plan."},
    ]
    attempt = capture.prepare_preview(note_id, "capture-order-key", "direct", selected)
    confirmed = capture.confirm(
        note_id,
        "capture-order-key",
        attempt.note_fingerprint,
        "Interview preparation notes",
        attempt.preview["blocks"],
    )
    with factory() as session:
        version = session.get(KnowledgeNoteVersion, confirmed.version_id)
        assert version is not None
        evidence_ids = list(
            session.scalars(
                select(KnowledgeEvidence.id)
                .join(KnowledgeNoteEvidence, KnowledgeNoteEvidence.evidence_id == KnowledgeEvidence.id)
                .where(KnowledgeNoteEvidence.note_version_id == version.id)
            )
        )
    assert len(evidence_ids) == 2

    with factory() as session:
        second_note = InterviewNote(
            application_id=ids[0],
            application_event_id=None,
            company="Acme",
            position="Backend",
            questions="Discuss observability.",
            self_reflection="I described the signal.",
            difficulty_points="Clarify the alert.",
            mood="steady",
        )
        session.add(second_note)
        session.commit()
        second_note_id = second_note.id
    second_attempt = capture.prepare_preview(
        second_note_id,
        "capture-order-key-2",
        "direct",
        [{"fragment_id": "questions", "path": "/questions", "start": 0, "end": 22, "text": "Discuss observability."}],
    )
    second_confirmed = capture.confirm(
        second_note_id,
        "capture-order-key-2",
        second_attempt.note_fingerprint,
        "Second interview notes",
        second_attempt.preview["blocks"],
    )
    with factory() as session:
        second_version = session.get(KnowledgeNoteVersion, second_confirmed.version_id)
        assert second_version is not None
        second_evidence_ids = list(
            session.scalars(
                select(KnowledgeEvidence.id)
                .join(KnowledgeNoteEvidence, KnowledgeNoteEvidence.evidence_id == KnowledgeEvidence.id)
                .where(KnowledgeNoteEvidence.note_version_id == second_version.id)
            )
        )
    assert len(second_evidence_ids) == 1

    repository = InterviewPreparationProposalsRepository(factory)
    first = repository.create_generated(
        application_id=ids[0],
        event_id=ids[1],
        resume_id=ids[2],
        jd_text=JD_TEXT,
        knowledge_selections=[
            {"note_version_id": second_version.id, "evidence_ids": second_evidence_ids},
            {"note_version_id": version.id, "evidence_ids": [evidence_ids[1]]},
            {"note_version_id": version.id, "evidence_ids": [evidence_ids[0]]},
        ],
        user_assertions=[],
        idempotency_key="knowledge-order-key-01",
        model=SafeEmptyModel(),
    )
    replay = repository.create_generated(
        application_id=ids[0],
        event_id=ids[1],
        resume_id=ids[2],
        jd_text=JD_TEXT,
        knowledge_selections=[
            {"note_version_id": version.id, "evidence_ids": [evidence_ids[0]]},
            {"note_version_id": version.id, "evidence_ids": [evidence_ids[1]]},
            {"note_version_id": second_version.id, "evidence_ids": second_evidence_ids},
        ],
        user_assertions=[],
        idempotency_key="knowledge-order-key-01",
        model=SafeEmptyModel(),
    )

    assert first.proposal is not None
    assert replay.proposal is not None
    assert replay.created is False
    assert replay.proposal.id == first.proposal.id
    with factory() as session:
        row = session.get(InterviewPreparationProposal, first.proposal.id)
        assert row is not None
        snapshot = json.loads(row.input_snapshot_json)
    ordered = snapshot["knowledge_evidence"]
    assert [(item["note_version_id"], item["id"]) for item in ordered] == sorted(
        (item["note_version_id"], item["id"]) for item in ordered
    )
    assert [item["provider_path"] for item in ordered] == [
        f"/knowledge_evidence/{index:03d}" for index in range(1, len(ordered) + 1)
    ]
    factory.kw["bind"].dispose()


def test_late_stale_owner_cannot_invalidate_ready_proposal(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)
    first = _generate(repository, ids, "late-owner-key-0001", SafeEmptyModel())
    assert first.proposal is not None

    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        snapshot = json.loads(row.input_snapshot_json)

    result = repository._call_and_store(
        model=SafeEmptyModel(),
        owner_revision=1,
        owner_token="stale-provider-token",
        application_id=ids[0],
        event_id=ids[1],
        resume_id=ids[2],
        jd_text=JD_TEXT,
        knowledge_selections=[],
        user_assertions=["I led the migration."],
        idempotency_key="late-owner-key-0001",
        source_fingerprint="stale-fingerprint",
        snapshot=snapshot,
        on_diagnostic=None,
    )

    assert result.pending is False
    assert result.proposal is not None
    assert result.proposal.id == first.proposal.id
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "ready"
    factory.kw["bind"].dispose()


def test_invalidated_attempt_is_never_returned_as_pending(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)
    with pytest.raises(InterviewPreparationProviderError):
        _generate(repository, ids, "invalidated-key-0001", FailingModel())
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        row.attempt_status = "invalidated"
        row.invalidation_reason = "source_conflict"
        session.commit()

    with pytest.raises(InterviewPreparationConflictError) as exc_info:
        _generate(repository, ids, "invalidated-key-0001", SafeEmptyModel())
    assert exc_info.value.code == "interview_preparation_attempt_invalidated"
    factory.kw["bind"].dispose()


def test_event_delete_keeps_history_readable_and_source_changed(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)
    _generate(repository, ids, "event-history-key-01", SafeEmptyModel())
    with factory() as session:
        session.execute(text("DELETE FROM application_events WHERE id=:id"), {"id": ids[1]})
        session.commit()

    history = repository.list(ids[0])
    assert history[0].source_status == "source_changed"
    factory.kw["bind"].dispose()


def test_resume_delete_keeps_history_readable_and_source_changed(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)
    _generate(repository, ids, "resume-history-key-1", SafeEmptyModel())
    with factory() as session:
        session.execute(text("DELETE FROM resumes WHERE id=:id"), {"id": ids[2]})
        session.commit()

    history = repository.list(ids[0])
    assert history[0].source_status == "source_changed"
    factory.kw["bind"].dispose()


def test_soft_deleted_application_returns_not_found_for_history(tmp_path) -> None:
    factory, ids = _setup(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)
    _generate(repository, ids, "application-history-key-1", SafeEmptyModel())
    with factory() as session:
        session.execute(
            text("UPDATE applications SET deleted_at=CURRENT_TIMESTAMP WHERE id=:id"),
            {"id": ids[0]},
        )
        session.commit()

    with pytest.raises(InterviewPreparationNotFound):
        repository.list(ids[0])
    factory.kw["bind"].dispose()


def _setup_selected_signal(tmp_path, *, focus_count: int = 1):  # type: ignore[no-untyped-def]
    factory = init_database(tmp_path / f"preparation-v2-{uuid4()}.sqlite3")
    focuses = [
        {
            "id": f"focus-{index}",
            "text": f"准备重点 {index}",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/difficulty_points",
                    "excerpt": "cache consistency tradeoffs",
                }
            ],
        }
        for index in range(1, focus_count + 1)
    ]
    seeded = seed_review_candidate(
        factory,
        focus_id="focus-1",
        practice_focuses=focuses,
    )
    version_ids = []
    for focus in focuses:
        _signal_id, version_id = _commit_signal(
            factory,
            seeded,
            focus_id=focus["id"],
            idempotency_key=str(uuid4()),
            user_note=f"用户备注 {focus['id']}",
        )
        version_ids.append(version_id)
    with factory() as session:
        target = ApplicationEvent(
            application_id=int(seeded["application_id"]),
            event_type="interview",
            subtype="system_design",
            round=3,
            scheduled_at=datetime(2031, 1, 2, 9, tzinfo=timezone.utc),
            duration_minutes=60,
            status="todo",
        )
        resume = Resume(
            title="Preparation Resume",
            name="Preparation Resume",
            parse_status="text-ready",
            content_json=json.dumps(
                {"experience": [{"highlights": ["Built reliable APIs"]}]}
            ),
        )
        session.add_all((target, resume))
        session.commit()
        target_id = target.id
        resume_id = resume.id
    return factory, seeded, tuple(version_ids), target_id, resume_id


def test_v1_snapshot_remains_byte_equal_to_pinned_c5a020c_fixture(tmp_path) -> None:
    from offerpilot.repositories.interview_preparation_proposals import (
        _build_v1_snapshot,
    )

    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "review_readiness"
            / "interview_preparation_v1_c5a020c.json"
        ).read_text(encoding="utf-8")
    )
    factory, ids = _setup(tmp_path)
    with factory() as session:
        version = ApplicationJDVersion(
            application_id=ids[0],
            version_number=3,
            jd_text=JD_TEXT,
            content_sha256="jd-content-sha256",
            source_kind="ui",
            idempotency_key="jd-version-v1-baseline-0001",
            request_fingerprint_sha256="jd-request-sha256",
        )
        session.add(version)
        session.commit()
        snapshot = _build_v1_snapshot(
            session,
            application_id=ids[0],
            event_id=ids[1],
            resume_id=ids[2],
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=["I led the migration."],
            jd_version_id=version.id,
        )
    assert canonical_json(snapshot) == fixture["input_snapshot_json"]
    assert sha256_text(canonical_json(snapshot)) == fixture["source_fingerprint"]
    request = {
        "event_id": ids[1],
        "idempotency_key": "review-readiness-prep-v1-0001",
        "jd_version_id": version.id,
        "knowledge_selections": [],
        "resume_id": ids[2],
        "user_assertions": ["I led the migration."],
    }
    assert canonical_json(request) == fixture["canonical_request_json"]
    assert sha256_text(canonical_json(request)) == fixture["canonical_request_sha256"]


def test_v2_request_input_and_selection_fingerprints_match_ordered_golden(
    tmp_path,
) -> None:
    from offerpilot.repositories.interview_preparation_proposals import (
        _build_v2_snapshot,
    )

    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(
        tmp_path, focus_count=2
    )
    ordered_ids = tuple(reversed(version_ids))
    request_identity = {
        "event_id": target_id,
        "idempotency_key": "v2-golden-order-0001",
        "knowledge_selections": [],
        "readiness_feedback_selection": {
            "present": True,
            "ordered_version_ids": list(ordered_ids),
        },
        "resume_id": resume_id,
        "user_assertions": [],
    }
    with factory() as session:
        snapshot = _build_v2_snapshot(
            session,
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            readiness_feedback_version_ids=ordered_ids,
        )
        forward_snapshot = _build_v2_snapshot(
            session,
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            readiness_feedback_version_ids=version_ids,
        )
    assert canonical_json(request_identity) == (
        '{"event_id":2,"idempotency_key":"v2-golden-order-0001",'
        '"knowledge_selections":[],"readiness_feedback_selection":'
        '{"ordered_version_ids":[2,1],"present":true},"resume_id":1,'
        '"user_assertions":[]}'
    )
    assert sha256_text(canonical_json(snapshot)) == (
        "f1dfbc72c0d67dfb6ed016c4c6b5e3638fcca82c9c6d6e7649bb2d3e9fe039fc"
    )
    assert snapshot["readiness_feedback_selection_fingerprint"] == (
        "sha256:e01e6b1b7694c479f1f3756373681a6518500b0b9a80c3b0239ea90aaf4f166b"
    )
    assert forward_snapshot["readiness_feedback_selection_fingerprint"] == (
        "sha256:9fc8aa910f3ce983b053d642df340a32eee689d0b60a74add0e2eb62d1202160"
    )
    assert sha256_text(canonical_json(forward_snapshot)) == (
        "17cb4250d9ef470f245fbd93253914290e980718e26daa70f89d22afcea64f01"
    )


def test_selection_loader_preserves_order_and_uses_no_signal_query_for_empty(tmp_path) -> None:
    from offerpilot.review_readiness.preparation_selection import (
        PreparationReadinessSelectionLoader,
    )

    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(
        tmp_path, focus_count=2
    )
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):  # type: ignore[no-untyped-def]
        statements.append(statement.lower())

    engine = factory.kw["bind"]
    sqlalchemy_event.listen(engine, "before_cursor_execute", capture)
    try:
        with factory() as session:
            empty = PreparationReadinessSelectionLoader(session).load(
                application_id=int(seeded["application_id"]),
                target_event_id=target_id,
                resume_id=resume_id,
                ordered_version_ids=(),
            )
        assert empty.ordered_version_ids == ()
        assert empty.readiness_feedback == ()
        assert not any("interview_readiness_signal" in item for item in statements)
        statements.clear()
        with factory() as session:
            selection = PreparationReadinessSelectionLoader(session).load(
                application_id=int(seeded["application_id"]),
                target_event_id=target_id,
                resume_id=resume_id,
                ordered_version_ids=tuple(reversed(version_ids)),
            )
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", capture)
    assert selection.ordered_version_ids == tuple(reversed(version_ids))
    assert [item.statement for item in selection.readiness_feedback] == [
        "准备重点 2",
        "准备重点 1",
    ]
    assert selection.selection_fingerprint.startswith("sha256:")
    assert all(not hasattr(item, "version_id") for item in selection.readiness_feedback)


def test_selection_loader_returns_deeply_immutable_copied_dto(tmp_path) -> None:
    from offerpilot.review_readiness.preparation_selection import (
        PreparationReadinessSelectionLoader,
    )

    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)
    with factory() as session:
        selection = PreparationReadinessSelectionLoader(session).load(
            application_id=int(seeded["application_id"]),
            target_event_id=target_id,
            resume_id=resume_id,
            ordered_version_ids=version_ids,
        )
    item = selection.readiness_feedback[0]
    assert is_dataclass(item)
    with pytest.raises(FrozenInstanceError):
        item.statement = "forged"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("target_completed", "preparation_readiness_target_not_eligible"),
        ("target_cancelled", "preparation_readiness_target_not_eligible"),
        ("target_unknown", "preparation_readiness_target_not_eligible"),
        ("target_wrong_type", "preparation_readiness_target_not_eligible"),
        ("source_equals_target", "preparation_readiness_target_not_eligible"),
        ("source_changed", "preparation_readiness_signal_not_current"),
        ("retracted", "preparation_readiness_signal_not_current"),
        ("missing", "preparation_readiness_signal_not_current"),
    ),
)
def test_selection_loader_fails_closed_for_invalid_source_or_target(
    tmp_path, mutation: str, expected_code: str
) -> None:
    from offerpilot.review_readiness.preparation_selection import (
        PreparationReadinessSelectionError,
        PreparationReadinessSelectionLoader,
    )

    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)
    with factory() as session:
        if mutation == "target_completed":
            target = session.get(ApplicationEvent, target_id)
            assert target is not None
            target.status = "completed"
        elif mutation == "target_cancelled":
            target = session.get(ApplicationEvent, target_id)
            assert target is not None
            target.status = "cancelled"
        elif mutation == "target_unknown":
            target = session.get(ApplicationEvent, target_id)
            assert target is not None
            target.status = "future_status"
        elif mutation == "target_wrong_type":
            target = session.get(ApplicationEvent, target_id)
            assert target is not None
            target.event_type = "written_test"
        elif mutation == "source_equals_target":
            target_id = int(seeded["event_id"])
        elif mutation == "source_changed":
            note = session.get(InterviewNote, int(seeded["note_id"]))
            assert note is not None
            note.content_revision += 1
        elif mutation == "retracted":
            signal = session.scalar(select(InterviewReadinessSignal))
            assert signal is not None
            signal.current_version_id = None
        else:
            version_ids = (2**62,)
        session.commit()
    with factory() as session, pytest.raises(PreparationReadinessSelectionError) as exc_info:
        PreparationReadinessSelectionLoader(session).load(
            application_id=int(seeded["application_id"]),
            target_event_id=target_id,
            resume_id=resume_id,
            ordered_version_ids=version_ids,
        )
    assert exc_info.value.code == expected_code


def test_selection_loader_accepts_eight_and_rejects_nine_or_cross_application(
    tmp_path,
) -> None:
    from offerpilot.review_readiness.preparation_selection import (
        PreparationReadinessSelectionError,
        PreparationReadinessSelectionLoader,
    )

    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(
        tmp_path, focus_count=8
    )
    with factory() as session:
        selection = PreparationReadinessSelectionLoader(session).load(
            application_id=int(seeded["application_id"]),
            target_event_id=target_id,
            resume_id=resume_id,
            ordered_version_ids=version_ids,
        )
    assert selection.ordered_version_ids == version_ids
    assert len(selection.readiness_feedback) == 8

    with factory() as session, pytest.raises(PreparationReadinessSelectionError):
        PreparationReadinessSelectionLoader(session).load(
            application_id=int(seeded["application_id"]),
            target_event_id=target_id,
            resume_id=resume_id,
            ordered_version_ids=version_ids + (2**62,),
        )

    with factory() as session:
        other = Application(company_name="Other", position_name="Backend", source="test")
        session.add(other)
        session.flush()
        other_target = ApplicationEvent(
            application_id=other.id,
            event_type="interview",
            subtype="system_design",
            round=4,
            scheduled_at=datetime(2032, 1, 1, 9, tzinfo=timezone.utc),
            duration_minutes=60,
            status="todo",
        )
        session.add(other_target)
        session.commit()
        other_application_id = other.id
        other_target_id = other_target.id
    with factory() as session, pytest.raises(PreparationReadinessSelectionError) as exc_info:
        PreparationReadinessSelectionLoader(session).load(
            application_id=other_application_id,
            target_event_id=other_target_id,
            resume_id=resume_id,
            ordered_version_ids=(version_ids[0],),
        )
    assert exc_info.value.code == "preparation_readiness_signal_not_current"


def _exact_feedback_high_water_fixture(target_bytes: int) -> list[dict[str, object]]:
    feedback: list[dict[str, object]] = []
    for _index in range(8):
        evidence = []
        for _evidence_index in range(5):
            excerpt = '中文"\\🙂'
            evidence.append(
                {
                    "path": "/difficulty_points",
                    "excerpt": excerpt,
                    "excerpt_sha256": "sha256:"
                    + hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                }
            )
        feedback.append(
            {
                "statement": "准备重点",
                "user_note": "备注",
                "source_event": {"round": 2, "subtype": "technical"},
                "practice_state": "completed",
                "evidence": evidence,
            }
        )
    wrapper = canonical_json({"readiness_feedback": feedback}).encode("utf-8")
    remaining = target_bytes - len(wrapper)
    assert remaining >= 0
    if remaining % 2:
        feedback[-1]["statement"] = str(feedback[-1]["statement"]) + "x"
        remaining -= 1
    slash_counts = [remaining // 2 // 40] * 40
    for index in range((remaining // 2) % 40):
        slash_counts[index] += 1
    for flat_index, slash_count in enumerate(slash_counts):
        item = feedback[flat_index // 5]
        evidence = item["evidence"]
        assert isinstance(evidence, list)
        evidence_item = evidence[flat_index % 5]
        assert isinstance(evidence_item, dict)
        excerpt = str(evidence_item["excerpt"]) + "\\" * slash_count
        evidence_item["excerpt"] = excerpt
        evidence_item["excerpt_sha256"] = "sha256:" + hashlib.sha256(
            excerpt.encode("utf-8")
        ).hexdigest()
    assert len(canonical_json({"readiness_feedback": feedback}).encode("utf-8")) == (
        target_bytes
    )
    return feedback


@pytest.mark.parametrize(("target_bytes", "accepted"), ((65_536, True), (65_537, False)))
def test_selection_loader_enforces_exact_wrapper_after_each_of_eight_full_signals(
    tmp_path, monkeypatch, target_bytes: int, accepted: bool
) -> None:
    import offerpilot.review_readiness.preparation_selection as preparation_selection

    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(
        tmp_path, focus_count=8
    )
    feedback = _exact_feedback_high_water_fixture(target_bytes)
    by_version_id = dict(zip(version_ids, feedback, strict=True))

    def project(_session, *, signal_version_id):  # type: ignore[no-untyped-def]
        item = by_version_id[signal_version_id]
        source_event = item["source_event"]
        assert isinstance(source_event, dict)
        evidence_json = item["evidence"]
        assert isinstance(evidence_json, list)
        aggregate = SimpleNamespace(
            application_id=int(seeded["application_id"]),
            source_event_id=int(seeded["event_id"]),
            version_id=signal_version_id,
            statement_text=item["statement"],
            user_note=item["user_note"],
            evidence=tuple(
                SimpleNamespace(
                    source_path=evidence_item["path"],
                    excerpt=evidence_item["excerpt"],
                    excerpt_sha256=evidence_item["excerpt_sha256"],
                )
                for evidence_item in evidence_json
            ),
            practice_source_fingerprint=f"sha256:{signal_version_id:064x}",
        )
        return SimpleNamespace(state="current", aggregate=aggregate)

    original_canonicalizer = preparation_selection.canonical_readiness_feedback_bytes
    canonicalizer_calls: list[int] = []

    def record_complete_prefix(items):  # type: ignore[no-untyped-def]
        canonicalizer_calls.append(len(items))
        return original_canonicalizer(items)

    monkeypatch.setattr(preparation_selection, "load_canonical_readiness_signal", project)
    monkeypatch.setattr(
        preparation_selection,
        "canonical_readiness_feedback_bytes",
        record_complete_prefix,
    )
    with factory() as session:
        if accepted:
            selection = preparation_selection.PreparationReadinessSelectionLoader(
                session
            ).load(
                application_id=int(seeded["application_id"]),
                target_event_id=target_id,
                resume_id=resume_id,
                ordered_version_ids=version_ids,
            )
            assert len(selection.canonical_readiness_feedback_json.encode("utf-8")) == (
                65_536
            )
        else:
            with pytest.raises(
                preparation_selection.PreparationReadinessSelectionError
            ) as exc_info:
                preparation_selection.PreparationReadinessSelectionLoader(session).load(
                    application_id=int(seeded["application_id"]),
                    target_event_id=target_id,
                    resume_id=resume_id,
                    ordered_version_ids=version_ids,
                )
            assert exc_info.value.code == "preparation_readiness_feedback_too_large"
    assert canonicalizer_calls == list(range(1, 9))


def test_repository_freezes_explicit_empty_v2_and_conflicts_with_absent_v1(tmp_path) -> None:
    factory, seeded, _version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)
    created = repository.create_generated(
        application_id=int(seeded["application_id"]),
        event_id=target_id,
        resume_id=resume_id,
        jd_text=JD_TEXT,
        knowledge_selections=[],
        user_assertions=[],
        idempotency_key="explicit-empty-v2-0001",
        model=SafeEmptyModel(),
        readiness_feedback_version_ids_present=True,
        readiness_feedback_version_ids=(),
    )
    assert created.proposal is not None
    with factory() as session:
        row = session.get(InterviewPreparationProposal, created.proposal.id)
        assert row is not None
        snapshot = json.loads(row.input_snapshot_json)
    assert snapshot["input_contract"] == "interview-preparation-input-v2"
    assert snapshot["readiness_feedback"] == []
    assert snapshot["readiness_feedback_selection"]["present"] is True
    assert snapshot["readiness_feedback_selection"]["ordered_version_ids"] == []
    assert snapshot["readiness_feedback_selection_fingerprint"].startswith("sha256:")

    with pytest.raises(InterviewPreparationConflictError) as exc_info:
        repository.create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key="explicit-empty-v2-0001",
            model=SafeEmptyModel(),
        )
    assert exc_info.value.code == "interview_preparation_idempotency_conflict"


@pytest.mark.parametrize("conflict_kind", ("absent", "different_selection"))
def test_v2_request_conflict_does_not_mutate_accepted_attempt_and_original_resumes(
    tmp_path, conflict_kind: str
) -> None:
    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(
        tmp_path, focus_count=2
    )
    repository = InterviewPreparationProposalsRepository(factory)
    key = f"v2-nondestructive-conflict-{conflict_kind}"
    original_selection = (version_ids[0],)
    with pytest.raises(InterviewPreparationProviderError):
        repository.create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key=key,
            model=FailingModel(),
            readiness_feedback_version_ids_present=True,
            readiness_feedback_version_ids=original_selection,
        )
    with factory() as session:
        accepted = session.scalar(select(InterviewPreparationProposal))
        assert accepted is not None
        accepted_state = (
            accepted.attempt_status,
            accepted.provider_call_token,
            accepted.generation_revision,
            accepted.provider_lease_until,
            accepted.input_snapshot_json,
            accepted.source_fingerprint,
        )

    conflict_kwargs = (
        {}
        if conflict_kind == "absent"
        else {
            "readiness_feedback_version_ids_present": True,
            "readiness_feedback_version_ids": (version_ids[1],),
        }
    )
    with pytest.raises(InterviewPreparationConflictError) as exc_info:
        repository.create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key=key,
            model=SafeEmptyModel(),
            **conflict_kwargs,
        )
    assert exc_info.value.code == "interview_preparation_idempotency_conflict"
    with factory() as session:
        unchanged = session.scalar(select(InterviewPreparationProposal))
        assert unchanged is not None
        assert (
            unchanged.attempt_status,
            unchanged.provider_call_token,
            unchanged.generation_revision,
            unchanged.provider_lease_until,
            unchanged.input_snapshot_json,
            unchanged.source_fingerprint,
        ) == accepted_state
        unchanged.provider_lease_until = datetime(2000, 1, 1)
        session.commit()

    resumed = repository.create_generated(
        application_id=int(seeded["application_id"]),
        event_id=target_id,
        resume_id=resume_id,
        jd_text=JD_TEXT,
        knowledge_selections=[],
        user_assertions=[],
        idempotency_key=key,
        model=SafeEmptyModel(),
        readiness_feedback_version_ids_present=True,
        readiness_feedback_version_ids=original_selection,
    )
    assert resumed.proposal is not None
    assert resumed.proposal.attempt_status == "ready"


def test_invalid_explicit_selection_fails_before_provider_call(tmp_path) -> None:
    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)
    with factory() as session:
        target = session.get(ApplicationEvent, target_id)
        assert target is not None
        target.status = "cancelled"
        session.commit()
    model = SafeEmptyModel()
    with pytest.raises(InterviewPreparationValidationError) as exc_info:
        InterviewPreparationProposalsRepository(factory).create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key="invalid-selection-v2-001",
            model=model,
            readiness_feedback_version_ids_present=True,
            readiness_feedback_version_ids=version_ids,
        )
    assert getattr(exc_info.value, "code", None) == (
        "interview_preparation_readiness_selection_invalid"
    )
    assert model.calls == 0


def test_target_drift_during_v2_provider_discards_late_result(tmp_path) -> None:
    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)

    class DriftModel(SafeEmptyModel):
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            with factory() as session:
                target = session.get(ApplicationEvent, target_id)
                assert target is not None
                target.status = "completed"
                session.commit()
            return super().complete(messages, tools)

    model = DriftModel()
    repository = InterviewPreparationProposalsRepository(factory)
    with pytest.raises(InterviewPreparationConflictError) as exc_info:
        repository.create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key="target-drift-v2-0001",
            model=model,
            readiness_feedback_version_ids_present=True,
            readiness_feedback_version_ids=version_ids,
        )
    assert exc_info.value.code == "interview_preparation_source_conflict"
    assert model.calls == 1
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "invalidated"
        assert row.proposal_json == ""


def test_selected_source_drift_during_provider_discards_late_result(tmp_path) -> None:
    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)

    class DriftModel(SafeEmptyModel):
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            with factory() as session:
                source = session.get(ApplicationEvent, int(seeded["event_id"]))
                assert source is not None
                source.status = "in_progress"
                session.commit()
            return super().complete(messages, tools)

    model = DriftModel()
    with pytest.raises(InterviewPreparationConflictError) as exc_info:
        InterviewPreparationProposalsRepository(factory).create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key="source-drift-v2-00001",
            model=model,
            readiness_feedback_version_ids_present=True,
            readiness_feedback_version_ids=version_ids,
        )
    assert exc_info.value.code == "interview_preparation_source_conflict"
    assert model.calls == 1


def test_selected_current_pointer_retraction_during_provider_discards_late_result(
    tmp_path,
) -> None:
    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)

    class RetractingModel(SafeEmptyModel):
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            with factory() as session:
                signal = session.scalar(select(InterviewReadinessSignal))
                assert signal is not None
                signal.current_version_id = None
                session.commit()
            return super().complete(messages, tools)

    model = RetractingModel()
    with pytest.raises(InterviewPreparationConflictError) as exc_info:
        InterviewPreparationProposalsRepository(factory).create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key="pointer-retract-v2-0001",
            model=model,
            readiness_feedback_version_ids_present=True,
            readiness_feedback_version_ids=version_ids,
        )
    assert exc_info.value.code == "interview_preparation_source_conflict"
    assert model.calls == 1
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "invalidated"
        assert row.proposal_json == ""


def test_application_visibility_drift_during_v2_provider_invalidates_attempt(tmp_path) -> None:
    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)

    class DriftModel(SafeEmptyModel):
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            with factory() as session:
                application = session.get(Application, int(seeded["application_id"]))
                assert application is not None
                application.deleted_at = datetime.now(timezone.utc)
                session.commit()
            return super().complete(messages, tools)

    with pytest.raises(InterviewPreparationConflictError) as exc_info:
        InterviewPreparationProposalsRepository(factory).create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key="application-drift-v2-01",
            model=DriftModel(),
            readiness_feedback_version_ids_present=True,
            readiness_feedback_version_ids=version_ids,
        )
    assert exc_info.value.code == "interview_preparation_source_conflict"
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "invalidated"


def test_repository_routes_explicit_selection_through_v2_provider_contract(tmp_path) -> None:
    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)

    class ReadinessModel:
        supports_json_schema = False

        def __init__(self) -> None:
            self.calls = 0
            self.messages = []

        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            self.calls += 1
            self.messages.append(messages)
            return Assistant(
                content=json.dumps(
                    {
                        **safe_empty_interview_preparation_proposal(),
                        "review_points": [
                            {
                                "id": "feedback-review-1",
                                "text": "准备重点复习",
                                "evidence_refs": [
                                    {
                                        "source": "confirmed_readiness_feedback",
                                        "path": "/readiness_feedback/0/evidence/0/excerpt",
                                        "excerpt": "cache consistency tradeoffs",
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            )

    model = ReadinessModel()
    result = InterviewPreparationProposalsRepository(factory).create_generated(
        application_id=int(seeded["application_id"]),
        event_id=target_id,
        resume_id=resume_id,
        jd_text=JD_TEXT,
        knowledge_selections=[],
        user_assertions=[],
        idempotency_key="repository-v2-provider-0001",
        model=model,
        readiness_feedback_version_ids_present=True,
        readiness_feedback_version_ids=version_ids,
    )
    assert result.proposal is not None
    assert result.proposal.proposal_status == "normal"
    assert model.calls == 1
    assert "cache consistency tradeoffs" in model.messages[0][1].content


def test_ready_v2_replay_uses_frozen_contract_after_source_drift(tmp_path) -> None:
    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)
    first = repository.create_generated(
        application_id=int(seeded["application_id"]),
        event_id=target_id,
        resume_id=resume_id,
        jd_text=JD_TEXT,
        knowledge_selections=[],
        user_assertions=[],
        idempotency_key="ready-v2-frozen-0001",
        model=SafeEmptyModel(),
        readiness_feedback_version_ids_present=True,
        readiness_feedback_version_ids=version_ids,
    )
    with factory() as session:
        source = session.get(ApplicationEvent, int(seeded["event_id"]))
        assert source is not None
        source.status = "in_progress"
        session.commit()

    replay_model = SafeEmptyModel()
    replay = repository.create_generated(
        application_id=int(seeded["application_id"]),
        event_id=target_id,
        resume_id=resume_id,
        jd_text=JD_TEXT,
        knowledge_selections=[],
        user_assertions=[],
        idempotency_key="ready-v2-frozen-0001",
        model=replay_model,
        readiness_feedback_version_ids_present=True,
        readiness_feedback_version_ids=version_ids,
    )
    assert replay.proposal is not None and first.proposal is not None
    assert replay.proposal.id == first.proposal.id
    assert replay.created is False
    assert replay_model.calls == 0


def test_expired_v2_provider_fallback_source_drift_invalidates_without_second_call(
    tmp_path,
) -> None:
    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)
    repository = InterviewPreparationProposalsRepository(factory)
    with pytest.raises(InterviewPreparationProviderError):
        repository.create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key="unknown-v2-frozen-01",
            model=FailingModel(),
            readiness_feedback_version_ids_present=True,
            readiness_feedback_version_ids=version_ids,
        )
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        source = session.get(ApplicationEvent, int(seeded["event_id"]))
        assert row is not None and source is not None
        row.provider_lease_until = datetime(2000, 1, 1)
        source.status = "in_progress"
        session.commit()

    retry_model = SafeEmptyModel()
    with pytest.raises(InterviewPreparationConflictError) as exc_info:
        repository.create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key="unknown-v2-frozen-01",
            model=retry_model,
            readiness_feedback_version_ids_present=True,
            readiness_feedback_version_ids=version_ids,
        )
    assert exc_info.value.code == "interview_preparation_source_conflict"
    assert retry_model.calls == 0
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.attempt_status == "invalidated"


def test_expired_v2_provider_fallback_reuses_byte_identical_frozen_provider_input(
    tmp_path,
) -> None:
    factory, seeded, version_ids, target_id, resume_id = _setup_selected_signal(tmp_path)

    class RecordingFailingModel(FailingModel):
        def __init__(self) -> None:
            super().__init__()
            self.messages = []

        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            self.messages.append(messages)
            return super().complete(messages, tools)

    class RecordingSafeEmptyModel(SafeEmptyModel):
        def __init__(self) -> None:
            super().__init__()
            self.messages = []

        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            self.messages.append(messages)
            return super().complete(messages, tools)

    repository = InterviewPreparationProposalsRepository(factory)
    first_model = RecordingFailingModel()
    with pytest.raises(InterviewPreparationProviderError):
        repository.create_generated(
            application_id=int(seeded["application_id"]),
            event_id=target_id,
            resume_id=resume_id,
            jd_text=JD_TEXT,
            knowledge_selections=[],
            user_assertions=[],
            idempotency_key="unknown-v2-unchanged-01",
            model=first_model,
            readiness_feedback_version_ids_present=True,
            readiness_feedback_version_ids=version_ids,
        )
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        frozen_snapshot_json = row.input_snapshot_json
        frozen_fingerprint = row.source_fingerprint
        row.provider_lease_until = datetime(2000, 1, 1)
        session.commit()

    retry_model = RecordingSafeEmptyModel()
    result = repository.create_generated(
        application_id=int(seeded["application_id"]),
        event_id=target_id,
        resume_id=resume_id,
        jd_text=JD_TEXT,
        knowledge_selections=[],
        user_assertions=[],
        idempotency_key="unknown-v2-unchanged-01",
        model=retry_model,
        readiness_feedback_version_ids_present=True,
        readiness_feedback_version_ids=version_ids,
    )
    assert result.proposal is not None
    assert result.proposal.attempt_status == "ready"
    assert first_model.calls == retry_model.calls == 1
    assert first_model.messages[0][1].content.encode("utf-8") == (
        retry_model.messages[0][1].content.encode("utf-8")
    )
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        assert row.input_snapshot_json == frozen_snapshot_json
        assert row.source_fingerprint == frozen_fingerprint
        assert row.generation_revision == 2
