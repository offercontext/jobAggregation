from datetime import datetime, timezone

import pytest

from offerpilot.db import init_database
from offerpilot.repositories.application_events import (
    ApplicationEventCreate,
    ApplicationEventsRepository,
)
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-14T15:00:00+08:00", "2026-09-14T07:00:00"),
        ("2026-09-14T00:30:00+08:00", "2026-09-13T16:30:00"),
        ("2026-09-14T23:30:00-05:00", "2026-09-15T04:30:00"),
        ("2026-09-14T07:00:00+00:00", "2026-09-14T07:00:00"),
        ("2026-09-14T07:00:00", "2026-09-14T07:00:00"),
    ],
)
def test_event_writes_preserve_instant_in_sqlite(tmp_path, bound, value, expected):
    factory = init_database(tmp_path / "time.db")
    application = ApplicationsRepository(factory).create(
        ApplicationCreate(company_name="云岚数据", position_name="AI工程师")
    )
    repository = ApplicationEventsRepository(factory)
    instant = datetime.fromisoformat(value)
    expected_utc = datetime.fromisoformat(expected)
    data = ApplicationEventCreate(
        application_id=application.id,
        event_type="interview",
        scheduled_at=instant,
        remind_at=instant,
        duration_minutes=60,
    )
    with factory() as session:
        writer = repository.bind(session) if bound else repository
        created = writer.create(data)
        assert created.scheduled_at == expected_utc
        assert created.remind_at == expected_utc
        # Update a different existing value, not a no-op roundtrip.
        original = writer.create(
            ApplicationEventCreate(
                application_id=application.id,
                event_type="interview",
                scheduled_at=datetime(2026, 1, 1),
                duration_minutes=30,
            )
        )
        updated = writer.update(original.id, data)
        assert updated is not None
        assert updated.scheduled_at == expected_utc
        assert updated.remind_at == expected_utc
        assert data.scheduled_at == instant  # Input remains untouched.
        if bound:
            session.rollback()
            assert repository.get(created.id) is None
    factory.kw["bind"].dispose()


def test_bound_event_collection_preserves_filters(tmp_path):
    session_factory = init_database(tmp_path / "data.db")
    application = ApplicationsRepository(session_factory).create(
        ApplicationCreate(company_name="A", position_name="Backend")
    )
    repository = ApplicationEventsRepository(session_factory)
    repository.create(
        ApplicationEventCreate(
            application_id=application.id,
            event_type="interview",
            scheduled_at=datetime(2026, 8, 20, 9, tzinfo=timezone.utc),
            duration_minutes=30,
        )
    )
    repository.create(
        ApplicationEventCreate(
            application_id=application.id,
            event_type="deadline",
            scheduled_at=datetime(2026, 9, 20, 9, tzinfo=timezone.utc),
            duration_minutes=30,
        )
    )

    with session_factory() as session:
        rows = repository.bind(session).list(month="2026-08", event_type="interview")

    assert len(rows) == 1
    assert rows[0].event.event_type == "interview"
