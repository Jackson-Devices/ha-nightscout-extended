"""Treatment age lookups with synthetic v1 and v3 snapshots."""

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ns_insulin_age_api",
    Path(__file__).resolve().parents[1] / "custom_components/nightscout_extended/api.py",
)
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)

NOW = datetime(2026, 1, 8, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(hours=84)
RECENT = NOW - timedelta(hours=6)
INSULIN = "Insulin Change"


def marker(row_id, when, **fields):
    return {
        "_id": row_id,
        "eventType": INSULIN,
        "created_at": when.isoformat(),
        "date": int(when.timestamp() * 1000),
        **fields,
    }


@pytest.mark.parametrize("v3", [False, True])
def test_age_warm_reconciles_old_marker_revisions_tombstones_and_prune(monkeypatch, v3):
    client = api.NightscoutClient(None, "https://fixture.invalid", None)
    client._use_v3 = v3
    rows = [
        marker("old", OLD),
        marker("future", NOW + timedelta(milliseconds=1)),
        marker("bolus", RECENT, eventType="Correction Bolus"),
        marker("site", RECENT, eventType="Site Change"),
        {"_id": "malformed", "eventType": INSULIN, "created_at": "invalid", "date": True},
    ]

    async def read(path, query, **_kwargs):
        if v3:
            assert path == "/api/v3/treatments"
            assert query == {"eventType$in": INSULIN, "limit": "100", "skip": "0"}
            return {"status": 200, "result": rows.copy()}
        assert path == "/api/v1/treatments.json"
        assert query["find[eventType][$in][0]"] == INSULIN
        assert query["count"] == "100"
        return rows.copy()

    monkeypatch.setattr(client, "_protected_v3" if v3 else "_get", read)

    def lookup(now=NOW):
        return asyncio.run(client.async_get_age_markers((INSULIN,), now))

    assert lookup()[INSULIN] == api.AgeMarker(OLD, INSULIN)
    rows.pop(1)
    assert lookup(NOW + timedelta(hours=1))[INSULIN].time == OLD
    rows[:] = []
    assert lookup() == {}
    rows[:] = [
        marker("edited", OLD, srvModified=1),
        marker("edited", RECENT, srvModified=2),
        marker("deleted", NOW, srvModified=2),
        {"_id": "deleted", "srvModified": 3, "isValid": False},
    ]
    assert lookup()[INSULIN].time == RECENT
    rows[:] = [{"_id": "exact", "eventType": INSULIN, "created_at": "2026-01-08T14:00:00+02:00"}]
    assert lookup()[INSULIN].time == NOW
    assert lookup()[INSULIN].time.tzinfo == timezone.utc
    rows[0] = {"_id": "numeric", "eventType": INSULIN, "mills": int(RECENT.timestamp() * 1000)}
    assert lookup()[INSULIN].time == RECENT


def test_legacy_age_query_overrides_native_four_day_cutoff(monkeypatch):
    client = api.NightscoutClient(None, "https://fixture.invalid", None)
    oldest = NOW - timedelta(days=7)
    rows = [marker("retained", oldest), marker("future", NOW + timedelta(milliseconds=1))]
    queries = []

    async def read(path, query):
        assert path == "/api/v1/treatments.json"
        queries.append(query)
        lower = query.get("find[created_at][$gte]", (NOW - timedelta(days=4)).isoformat())
        upper = query.get("find[created_at][$lte]", datetime.max.replace(tzinfo=timezone.utc).isoformat())
        return [
            row for row in rows
            if datetime.fromisoformat(lower) <= datetime.fromisoformat(row["created_at"])
            <= datetime.fromisoformat(upper)
        ][:int(query["count"])]

    monkeypatch.setattr(client, "_get", read)
    assert asyncio.run(client.async_get_age_markers((INSULIN,), NOW))[INSULIN].time == oldest
    assert queries == [{
        "find[eventType][$in][0]": INSULIN,
        "find[created_at][$gte]": "0001-01-01T00:00:00.000Z",
        "find[created_at][$lte]": "2026-01-08T12:00:00.000Z",
        "count": "100",
    }]


def test_v3_age_merges_bounded_pages_before_deciding_latest(monkeypatch):
    client = api.NightscoutClient(None, "https://fixture.invalid", None)
    client._use_v3 = True
    monkeypatch.setattr(api, "_V3_PAGE_SIZE", 2)
    calls = []
    pages = [
        [marker("older", OLD), marker("deleted", NOW, srvModified=2)],
        [{"_id": "deleted", "isValid": False, "srvModified": 3}],
    ]

    async def read(path, query):
        assert path == "/api/v3/treatments"
        calls.append(query)
        return {"status": 200, "result": pages.pop(0)}

    monkeypatch.setattr(client, "_protected_v3", read)
    assert asyncio.run(client.async_get_age_markers((INSULIN,), NOW))[INSULIN].time == OLD
    assert [query["skip"] for query in calls] == ["0", "2"]


@pytest.mark.parametrize("v3", [False, True])
def test_saturated_age_lookup_does_not_report_missing_or_partial(monkeypatch, v3):
    client = api.NightscoutClient(None, "https://fixture.invalid", None)
    client._use_v3 = v3
    monkeypatch.setattr(api, "_V3_PAGE_SIZE", 1)
    monkeypatch.setattr(api, "_V3_MAX_PAGES", 1)
    monkeypatch.setattr(api, "_AGE_LEGACY_COUNTS", (1, 2))
    calls = []

    async def read(path, query):
        page_size = int(query["limit" if v3 else "count"])
        calls.append(page_size)
        rows = [marker(f"change-{i}", OLD) for i in range(page_size)]
        return {"status": 200, "result": rows} if v3 else rows

    monkeypatch.setattr(client, "_protected_v3" if v3 else "_get", read)
    with pytest.raises(api.NightscoutConnectionError):
        asyncio.run(client.async_get_age_markers((INSULIN,), NOW))
    assert calls == ([1] if v3 else [1, 2])
