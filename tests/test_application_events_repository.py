from datetime import datetime, timezone

from offerpilot.db import init_database
from offerpilot.repositories.application_events import (
    ApplicationEventCreate,
    ApplicationEventsRepository,
)
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository


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
