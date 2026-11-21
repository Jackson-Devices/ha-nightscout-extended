"""Protocol-specific treatment identity and quantity regressions."""

import asyncio
from datetime import datetime, timedelta, timezone

from test_api import _V3Fixture, _v3_server, api


def test_legacy_duplicate_backfill_uses_explicit_identity_and_retains_anonymous_rows():
    at = "2026-01-01T12:00:00.000Z"
    original = {
        "_id": "dose", "identifier": "dose-alias", "created_at": at, "eventType": "Meal Bolus",
        "insulin": "1.5", "srvModified": 100,
    }
    edit = {
        "identifier": "dose-alias", "created_at": at, "eventType": "Meal Bolus",
        "insulin": "2.5", "srvModified": 200,
    }
    independent = {
        "_id": "separate", "created_at": at, "eventType": "Meal Bolus",
        "carbs": "24.5", "insulin": 0,
    }
    untracked = {"created_at": at, "eventType": "Note"}
    doomed = {"_id": "deleted", "created_at": at, "insulin": 999, "srvModified": 300}
    tombstone = {"_id": "deleted", "isValid": False, "srvModified": 300}

    first = api.parse_treatments([original, independent])
    reread = api.parse_treatments([
        original, independent, original, edit, doomed, tombstone,
        {**untracked, "notes": "Unknown identity A"}, {**untracked, "notes": "Unknown identity B"},
    ])
    assert [row.uid for row in first] == ["dose", "separate"]
    assert [row.uid for row in reread[:2]] == ["dose", "separate"]
    assert len(reread) == 4
    assert [row.notes for row in reread[2:]] == ["Unknown identity A", "Unknown identity B"]
    assert first[0].insulin == 1.5 and reread[0].insulin == 2.5
    assert reread[1].carbs == 24.5
    data = api.NightscoutData(treatments=reread)
    after = datetime(2026, 1, 1, 13, tzinfo=timezone.utc)
    assert data.last_treatment("insulin", after).uid == "dose"
    assert data.last_treatment("carbs", after).uid == "separate"


def test_v3_treatment_quantities_require_finite_json_numbers_without_losing_events():
    fixture = _V3Fixture()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    at = int(start.timestamp() * 1_000)
    rows = [
        {"identifier": "correct", "date": at + 1_000, "insulin": 2.5, "carbs": 12.75, "duration": 20.5},
        {"identifier": "zero", "date": at + 2_000, "insulin": 0, "carbs": 0},
        {"identifier": "strings", "date": at + 3_000, "insulin": "7.25", "carbs": "31.2",
         "duration": "60", "eventType": "Note", "notes": "Reading retained", "enteredBy": "Fixture"},
        {"identifier": "invalid", "date": at + 4_000, "insulin": True, "carbs": float("inf"), "duration": {}},
    ]
    fixture.treatment_page = lambda query: list(reversed(rows)) if "date$gte" in query else []

    async def exercise(client):
        await client.async_get_status()
        return await client.async_get_treatments(start, end)

    treatments = asyncio.run(_v3_server(fixture.handle, exercise))
    assert [t.uid for t in treatments] == ["correct", "zero", "strings", "invalid"]
    assert (treatments[0].insulin, treatments[0].carbs, treatments[0].duration_min) == (2.5, 12.75, 20.5)
    assert treatments[2].summary == "Reading retained" and treatments[2].entered_by == "Fixture"
    assert all(t.insulin is None and t.carbs is None and t.duration_min is None for t in treatments[1:])
    data = api.NightscoutData(treatments=treatments)
    assert data.last_treatment("insulin", end).uid == "correct"
    assert data.last_treatment("carbs", end).uid == "correct"
