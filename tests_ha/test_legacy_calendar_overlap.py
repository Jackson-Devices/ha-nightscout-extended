"""Exercise legacy Nightscout calendar reads through Home Assistant's calendar service."""

from datetime import datetime

import pytest
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.nightscout_extended.const import DOMAIN

URL = "https://ns.example.com"
TREATMENTS = f"{URL}/api/v1/treatments.json"
ENTITY = "calendar.ns_treatments"
START = "2026-11-22T12:34:00+00:00"
END = "2026-11-22T12:54:00+00:00"


def treatment(uid, clock, event_type="Note", **fields):
    return {
        "_id": uid, "created_at": f"2026-11-22T{clock}Z", "eventType": event_type, **fields,
    }


def treatment_response(rows, requests, *, status=200):
    """Model the date window, numeric filter, tuple sort and count of stock v1."""

    async def reply(method, url, data):
        query = dict(url.query)
        requests.append(query)
        if status != 200:
            return AiohttpClientMockResponse(method, url, status=status, json=[])

        assert "skip" not in query
        assert "find[duration][$gt]" not in query  # stock leaves the operand a string
        result = rows
        if "find[duration][$type]" in query:
            assert query["find[duration][$type]"] == "number"
            result = [row for row in result if type(row.get("duration")) in (int, float)]
        for operator, compare in (
            ("gte", lambda value, limit: value >= limit),
            ("lt", lambda value, limit: value < limit),
            ("lte", lambda value, limit: value <= limit),
        ):
            if (bound := query.get(f"find[created_at][${operator}]")) is not None:
                limit = datetime.fromisoformat(bound)
                result = [
                    row for row in result if compare(datetime.fromisoformat(row["created_at"]), limit)
                ]
        if "sort[0][0]" in query:
            assert query["sort[0][0]"] == "duration" and query["sort[0][1]"] == "desc"
            assert "find[created_at][$lt]" in query
            result = sorted(result, key=lambda row: row["duration"], reverse=True)
        else:
            result = sorted(result, key=lambda row: row["created_at"], reverse=True)
        return AiohttpClientMockResponse(method, url, json=result[: int(query["count"])])

    return reply


@pytest.fixture
async def calendar_site(hass: HomeAssistant, aioclient_mock, freezer):
    freezer.move_to("2026-11-22T13:44:00+00:00")
    props = {"bgnow": {"last": 150}}
    aioclient_mock.get(f"{URL}/api/v1/status.json", json={"version": "15.0.6", "settings": {}})
    aioclient_mock.get(f"{URL}/api/v2/properties/bgnow", json=props)
    aioclient_mock.get(
        f"{URL}/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal", json=props
    )
    aioclient_mock.get(TREATMENTS, json=[treatment("cached", "12:40:00.000", "Cached poll")])
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, title="NS", data={CONF_URL: URL})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    aioclient_mock.clear_requests()
    yield aioclient_mock
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def calendar_events(hass, end=END):
    response = await hass.services.async_call(
        "calendar", "get_events",
        {"entity_id": ENTITY, "start_date_time": START, "end_date_time": end},
        blocking=True, return_response=True,
    )
    return response[ENTITY]["events"]


@pytest.mark.parametrize("invalid_durations", [False, True], ids=["stock-overlap", "malformed-duration"])
async def test_calendar_includes_earlier_temp_basal_and_preserves_boundaries(
    hass, calendar_site, invalid_durations,
):
    requests = []
    rows = [
        treatment("temp-basal", "12:14:00.000", "Temp Basal", duration=30, enteredBy="Pump"),
        treatment("ends-at-start", "12:19:00.000", duration=15),
        treatment("partition-boundary", "12:29:00.000", "Short duration", duration=10),
        treatment("brief", "12:31:00.000", "Five-minute point"),
        treatment("inside", "12:44:00.000", "Inside", duration=1e18 if invalid_durations else 0),
        treatment("last-ms", "12:53:59.999", "Last millisecond"),
        treatment("starts-at-end", "12:54:00.000", "At the upper boundary"),
    ]
    if invalid_durations:
        rows.extend([
            treatment("overflow", "12:04:00.000", duration=1e18),
            treatment("end-overflow", "12:05:00.000", duration=5_000_000_000),
        ])
    calendar_site.get(TREATMENTS, side_effect=treatment_response(rows, requests))

    events = await calendar_events(hass)
    assert [(event["summary"], event["start"], event["end"]) for event in events] == [
        ("Temp Basal", "2026-11-22T12:14:00+00:00", "2026-11-22T12:44:00+00:00"),
        ("Short duration", "2026-11-22T12:29:00+00:00", "2026-11-22T12:39:00+00:00"),
        ("Five-minute point", "2026-11-22T12:31:00+00:00", "2026-11-22T12:36:00+00:00"),
        ("Inside", "2026-11-22T12:44:00+00:00", "2026-11-22T12:49:00+00:00"),
        ("Last millisecond", "2026-11-22T12:53:59.999000+00:00", "2026-11-22T12:58:59.999000+00:00"),
    ]
    assert events[0]["description"] == "Entered by Pump"
    assert any(
        query.get("find[created_at][$gte]") == "2026-11-22T12:04:00.000Z"
        and query.get("find[created_at][$lt]") == "2026-11-22T12:29:00.000Z"
        and query.get("find[duration][$type]") == "number"
        for query in requests
    )
    events = await calendar_events(hass, "2026-11-22T12:54:00.000001+00:00")
    assert events[-1]["summary"] == "At the upper boundary"


async def test_calendar_widens_full_legacy_range(hass, calendar_site):
    requests = []
    rows = [
        treatment(f"recent-{index}", "12:47:00.000", f"Recent {index}") for index in range(1000)
    ]
    rows.append(treatment("oldest", "12:35:00.000", "Past the first page"))
    calendar_site.get(TREATMENTS, side_effect=treatment_response(rows, requests))

    events = await calendar_events(hass)
    assert len(events) == 1001
    assert events[0]["summary"] == "Past the first page"
    assert [int(query["count"]) for query in requests if "find[created_at][$lte]" in query] == [1000, 10000]


@pytest.mark.parametrize("failure", ["transport", "incomplete"])
async def test_calendar_uses_last_poll_if_authoritative_read_fails(hass, calendar_site, caplog, failure):
    requests = []
    rows = [treatment("oldest", "12:14:00.000", "Authoritative only", duration=30)]
    if failure == "incomplete":
        rows *= 10000
    calendar_site.get(
        TREATMENTS, side_effect=treatment_response(rows, requests, status=503 if failure == "transport" else 200),
    )

    assert [event["summary"] for event in await calendar_events(hass)] == ["Cached poll"]
    if failure == "incomplete":
        assert any(query["count"] == "10000" and "find[created_at][$lt]" in query for query in requests)
        assert "Nightscout calendar read incomplete; using the last successful poll" in caplog.text
