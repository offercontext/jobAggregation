from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from offerpilot.api import create_app


def test_calendar_preserves_naive_utc_like_event_detail(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from offerpilot.repositories.application_events import ApplicationEventsRepository

    class StoredUtc(datetime):
        def astimezone(self, tz=None):
            if self.tzinfo is None:
                raise AssertionError("database UTC must not use the host timezone")
            return super().astimezone(tz)

    row = SimpleNamespace(
        event=SimpleNamespace(
            id=1, application_id=7, event_type="interview",
            scheduled_at=StoredUtc(2026, 9, 8, 6), duration_minutes=60, location="线上",
        ), company_name="星河智能", position_name="开发",
    )
    monkeypatch.setattr(ApplicationEventsRepository, "list", lambda *args, **kwargs: [row])
    client = TestClient(create_app(data_dir=tmp_path))
    result = client.get("/api/calendar?month=2026-09")
    assert result.status_code == 200
    assert result.json()[0]["scheduled_at"] == "2026-09-08T06:00:00Z"


def test_calendar_includes_applications_and_events(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    app = client.post(
        "/api/applications",
        json={"company_name": "ByteDance", "position_name": "Backend"},
    ).json()
    applied_at = datetime.fromisoformat(app["applied_at"].replace("Z", "+00:00"))
    if applied_at.tzinfo is None:
        applied_at = applied_at.replace(tzinfo=timezone.utc)
    else:
        applied_at = applied_at.astimezone(timezone.utc)
    month_start = applied_at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month_start = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    calendar_month = month_start.strftime("%Y-%m")
    client.post(
        "/api/application-events",
        json={
            "application_id": app["id"],
            "event_type": "written_test",
            "scheduled_at": (month_start + timedelta(days=9, hours=10)).isoformat().replace("+00:00", "Z"),
            "duration_minutes": 60,
            "location": "online",
        },
    )
    client.post(
        "/api/application-events",
        json={
            "application_id": app["id"],
            "event_type": "interview",
            "scheduled_at": (next_month_start + timedelta(hours=10)).isoformat().replace("+00:00", "Z"),
            "duration_minutes": 45,
            "location": "online",
        },
    )

    response = client.get("/api/calendar", params={"month": calendar_month})

    assert response.status_code == 200
    entries = response.json()
    assert any(entry["type"] == "applied" and entry["app_id"] == app["id"] for entry in entries)
    event_entry = next(entry for entry in entries if entry["type"] == "written_test")
    assert event_entry["title"] == "ByteDance · 笔试"
    assert event_entry["event_type"] == "written_test"
    assert event_entry["duration_minutes"] == 60
    assert event_entry["editable"] is True
    assert not any(
        entry.get("event_type") == "interview"
        and entry.get("scheduled_at")
        == (next_month_start + timedelta(hours=10)).isoformat().replace("+00:00", "Z")
        for entry in entries
    )


def test_calendar_bad_month_defaults_to_current_month(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.get("/api/calendar", params={"month": "bad"})

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_calendar_includes_interview_notes(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    note = client.post(
        "/api/notes",
        json={
            "company": "ByteDance",
            "position": "Backend",
            "round": "一面",
            "date": "2026-07-12",
        },
    ).json()

    response = client.get("/api/calendar", params={"month": "2026-07"})

    assert response.status_code == 200
    entry = next(item for item in response.json() if item.get("note_id") == note["id"])
    assert entry["type"] == "interview"
    assert entry["title"] == "ByteDance · 一面"
    assert entry["subtitle"] == "Backend"
