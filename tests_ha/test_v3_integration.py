"""Exercise Nightscout protocol selection through real Home Assistant entry setup."""

from datetime import datetime
import re
from time import time

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL, EVENT_HOMEASSISTANT_FINAL_WRITE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.nightscout_extended.age import AGE_SIGNALS
from custom_components.nightscout_extended.const import CONF_USE_API_SECRET, DOMAIN

URL = "https://parity.example.invalid"
TOKEN = "fixture-subject"
BEARER = "fixture-bearer"
# RFC 3174 section 7.3 TEST1 (https://www.rfc-editor.org/rfc/rfc3174#section-7.3).
LEGACY_WIRE_INPUT = "abc"
LEGACY_WIRE_SHA1 = "a9993e364706816aba3e25717850c26c9cd0d89d"
VERSION = "/api/v3/version"
EXCHANGE = f"/api/v2/authorization/request/{TOKEN}"
ENTRIES_V1 = "/api/v1/entries.json"
ENTRIES_V3 = "/api/v3/entries"
LAST_MODIFIED = "/api/v3/lastModified"
HISTORY_V3 = f"{ENTRIES_V3}/history/"
TREATMENTS_V3 = "/api/v3/treatments"
TREATMENTS_V1 = "/api/v1/treatments.json"
DEVICESTATUS_V1 = "/api/v1/devicestatus/"
DEVICESTATUS_V3 = "/api/v3/devicestatus"
PROPERTIES = "/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal"
READING = "2026-01-08T12:00:00+00:00"
V3_READING = "2026-01-08T12:07:00+00:00"
EARLY = "2026-01-08T12:10:00+00:00"
AFTERNOON = "2026-01-08T17:00:00+00:00"
AFTERNOON_READING = "2026-01-08T16:55:00+00:00"
AFTERNOON_FORECAST = (("2026-01-08T17:10:02+00:00", 207), ("2026-01-08T17:25:02+00:00", 231))
CALENDAR = "calendar.parity_site_treatments"
FORECAST = (("2026-01-08T12:15:02+00:00", 207), ("2026-01-08T12:30:02+00:00", 231))

LEGACY_TREATMENTS = (
    {
        "_id": "carbs-1",
        "created_at": "2026-01-08T12:20:00+00:00",
        "eventType": "Carb Correction",
        "carbs": 30,
        "notes": "Lunch",
        "enteredBy": "Fixture",
    },
    {
        "_id": "insulin-1",
        "created_at": "2026-01-08T12:25:00+00:00",
        "eventType": "Correction Bolus",
        "insulin": 0.5,
        "duration": 10,
    },
    {
        "_id": "insulin-zero",
        "created_at": "2026-01-08T12:35:00+00:00",
        "eventType": "Correction Bolus",
        "insulin": 0,
    },
    {
        "_id": "meal-next",
        "created_at": "2026-01-08T18:00:00+00:00",
        "eventType": "Meal Bolus",
        "carbs": 45,
    },
)


def _ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _mock_v1_sgv(mocker, entries=(), *, status=200):
    """Serve a legacy snapshot using the requested measurement-time bounds."""
    path = re.escape(URL + ENTRIES_V1)

    async def entries_response(method, url, _data):
        query = url.query
        assert query["find[type]"] == "sgv"
        assert set(query) - {"token"} == {
            "find[type]", "find[date][$gte]", "find[date][$lt]", "count",
        }
        start, end = int(query["find[date][$gte]"]), int(query["find[date][$lt]"])
        assert start < end
        count = int(query["count"])
        assert count > 0
        rows = sorted(
            (row for row in entries if start <= row["date"] < end),
            key=lambda row: row["date"], reverse=True,
        )
        return AiohttpClientMockResponse(method, url, status=status, json=rows[:count])

    mocker.get(re.compile(rf"^{path}\?"), side_effect=entries_response)


def _mock_v3_sgv(mocker, entries, *, deleted=()):
    """Serve separate measurement SEARCH and storage HISTORY over the same records."""

    def reply(method, url, result):
        return AiohttpClientMockResponse(method, url, status=200, json={"status": 200, "result": result})

    async def search_response(method, url, _data):
        query = url.query
        assert set(query) == {"type", "date$gte", "date$lt", "sort", "limit", "skip"}
        assert query["type"] == "sgv" and query["sort"] == "date"
        start, end = int(query["date$gte"]), int(query["date$lt"])
        skip, limit = int(query["skip"]), int(query["limit"])
        assert start < end and skip >= 0 and limit > 0
        rows = sorted(
            (row for row in entries if start <= row["date"] < end),
            key=lambda row: (row["date"], row["identifier"]),
        )
        return reply(method, url, rows[skip:skip + limit])

    async def history_response(method, url, _data):
        assert set(url.query) == {"limit"}
        boundary = int(url.path.removeprefix(HISTORY_V3))
        limit = int(url.query["limit"])
        assert limit > 0
        rows = sorted(
            (row for row in (*entries, *deleted) if row["srvModified"] > boundary),
            key=lambda row: row["srvModified"],
        )
        return reply(method, url, rows[:limit])

    async def last_modified_response(method, url, _data):
        assert not url.query
        marker = max(
            (max(row.get("date", 0), row["srvModified"]) for row in (*entries, *deleted)),
            default=None,
        )
        return reply(method, url, {
            "srvDate": int(time() * 1000),
            "collections": {"entries": marker} if marker is not None else {},
        })

    mocker.get(re.compile(rf"^{re.escape(URL + ENTRIES_V3)}\?"), side_effect=search_response)
    mocker.get(re.compile(rf"^{re.escape(URL + HISTORY_V3)}\d+\?"), side_effect=history_response)
    mocker.get(f"{URL}{LAST_MODIFIED}", side_effect=last_modified_response)


def _mock_site(
    mocker: AiohttpClientMocker, *, v3=False, v3_reading=READING, v3_glucose=153,
    forecast=FORECAST, treatments=LEGACY_TREATMENTS, reservoir=(), reservoir_status=200,
    reading=READING,
):
    """Install responses at the HTTP boundary of HA's existing aiohttp fixture."""
    mocker.clear_requests()
    mocker.get(
        f"{URL}/api/v1/status.json",
        json={"version": "15.0.9", "settings": {"customTitle": "Parity Site", "units": "mg/dl"}},
    )
    bgnow = {"last": 153, "mills": _ms(reading)}
    mocker.get(f"{URL}/api/v2/properties/bgnow", json={"bgnow": bgnow})
    props = {
        "bgnow": bgnow,
        "delta": {"mgdl": 7},
        "direction": {"value": "FortyFiveUp"},
        "iob": {"iob": 1.35, "source": "fixture-pump"},
        "cob": {"cob": 28.9, "source": "fixture-meal"},
        "basal": {"current": {"totalbasal": 0.75}},
    }
    if forecast is not None:
        props["ar2"] = {
            "forecast": {"predicted": [{"mills": _ms(t), "mgdl": bg} for t, bg in forecast]}
        }
    mocker.get(f"{URL}{PROPERTIES}", json=props)
    _mock_reservoir(mocker, v3, reservoir, reservoir_status)
    if not v3:
        mocker.get(f"{URL}{VERSION}", status=404)
        # Public and stored-secret entries can read v2 properties despite raw denial.
        _mock_v1_sgv(mocker, status=403)
        mocker.get(
            f"{URL}{TREATMENTS_V1}", params={"sort[0][0]": "duration", "sort[0][1]": "desc"}, json=[],
        )
        mocker.get(f"{URL}{TREATMENTS_V1}", json=list(treatments))
        return

    now = int(time())
    mocker.get(
        f"{URL}{VERSION}",
        json={"status": 200, "result": {"version": "15.0.9", "apiVersion": "3.0.5"}},
    )
    mocker.get(f"{URL}{EXCHANGE}", json={"token": BEARER, "iat": now, "exp": now + 86400})
    _mock_v3_sgv(mocker, ({
        "identifier": "parity-sgv",
        "type": "sgv",
        "sgv": v3_glucose,
        "date": _ms(v3_reading),
        "direction": "FortyFiveUp",
        "srvModified": _ms(v3_reading) + 1000,
    },))
    modern = []
    for row in reversed(treatments[1:]):
        treatment = dict(row)
        treatment["identifier"] = treatment.pop("_id")
        treatment["date"] = _ms(treatment.pop("created_at"))
        modern.append(treatment)
    mocker.get(
        f"{URL}{TREATMENTS_V3}", params={"sort$desc": "duration"},
        json={"status": 200, "result": [{"duration": 10}]},
    )
    path = re.escape(URL + TREATMENTS_V3)

    async def age_response(method, url, _data):
        kinds = url.query["eventType$in"].split("|")
        matches = [row for row in (*modern, treatments[0]) if row.get("eventType") in kinds]
        start, limit = int(url.query["skip"]), int(url.query["limit"])
        return AiohttpClientMockResponse(
            method, url, status=200, json={"status": 200, "result": matches[start:start + limit]},
        )

    age_query = re.compile(rf"^{path}\?(?:[^#]*&)?eventType(?:\$|%24)in=")
    mocker.get(age_query, side_effect=age_response)
    for field, results in (
        ("date", modern),
        ("created_at", [treatments[0]]),
    ):
        query = re.compile(rf"^{path}\?(?:[^#]*&)?{field}(?:\$|%24)gte=")
        mocker.get(query, json={"status": 200, "result": results})


def _mock_reservoir(mocker, v3, rows=(), status=200):
    path = DEVICESTATUS_V3 if v3 else DEVICESTATUS_V1
    payload = {"status": status, "result": list(rows)} if v3 else list(rows)
    mocker.get(f"{URL}{path}", json=payload, status=status)


async def _configure(hass):
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert flow["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_URL: "parity.example.invalid/", CONF_API_KEY: TOKEN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert entry.title == "Parity Site"
    assert entry.unique_id == URL
    assert entry.data == {CONF_URL: URL, CONF_API_KEY: TOKEN, CONF_USE_API_SECRET: False}
    return entry


def _state(hass, name):
    state = hass.states.get(f"sensor.parity_site_{name}")
    assert state is not None
    return state


def _entity_ids(hass, entry):
    return {
        registered.unique_id: registered.entity_id
        for registered in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


def _device_id(hass, entry):
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    assert devices[0].identifiers == {(DOMAIN, URL)}
    return devices[0].id


def _assert_wire_mode(mocker, *, v3, token=None, secret=None, discovery=False, bootstrap=False):
    calls = mocker.mock_calls
    paths = {url.path for _, url, _, _ in calls}
    assert PROPERTIES in paths
    if discovery:
        assert VERSION in paths
    v3_reads = {ENTRIES_V3, TREATMENTS_V3, LAST_MODIFIED, DEVICESTATUS_V3}
    history_paths = {path for path in paths if path.startswith(HISTORY_V3)}
    if v3:
        assert {TREATMENTS_V3, DEVICESTATUS_V3} <= paths
        assert history_paths
        assert paths.isdisjoint({ENTRIES_V1, TREATMENTS_V1, DEVICESTATUS_V1})
        if bootstrap:
            series_paths = [
                url.path for _, url, _, _ in calls
                if url.path in {ENTRIES_V3, LAST_MODIFIED} or url.path in history_paths
            ]
            assert len(series_paths) == 3
            assert series_paths[:2] == [LAST_MODIFIED, ENTRIES_V3]
            assert series_paths[2].startswith(HISTORY_V3)
    else:
        assert not history_paths
        assert paths.isdisjoint(v3_reads | {EXCHANGE})
        assert {ENTRIES_V1, TREATMENTS_V1, DEVICESTATUS_V1} <= paths
    for method, url, _, headers in calls:
        assert method.upper() == "GET"
        assert url.host == "parity.example.invalid"
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        if url.path in v3_reads or url.path in history_paths:
            assert headers["authorization"] == f"Bearer {BEARER}"
            assert "api-secret" not in headers and "token" not in url.query
        elif url.path in {VERSION, EXCHANGE}:
            assert "authorization" not in headers and "api-secret" not in headers
            assert "token" not in url.query
        else:
            assert "authorization" not in headers
            assert url.query.get("token") == token
            assert headers.get("api-secret") == secret


async def _refresh(hass, entry):
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.last_update_success


def _assert_snapshot(hass, *, reading):
    glucose = _state(hass, "glucose")
    assert glucose.attributes["unit_of_measurement"] == "mg/dL"
    assert glucose.attributes["direction"] == "FortyFiveUp"
    assert glucose.attributes["delta"] == 7
    assert glucose.attributes["thresholds_mgdl"] == {
        "urgent_low": 55, "low": 80, "high": 180, "urgent_high": 260,
    }
    assert {
        name: _state(hass, name).state
        for name in (
            "glucose", "glucose_change", "glucose_trend", "last_reading", "insulin_on_board",
            "carbs_on_board", "last_carbs", "last_insulin", "forecast_in_15_minutes",
            "forecast_in_30_minutes",
        )
    } == {
        "glucose": "153", "glucose_change": "7", "glucose_trend": "forty_five_up",
        "last_reading": reading, "insulin_on_board": "1.35", "carbs_on_board": "28.9",
        "last_carbs": "30", "last_insulin": "0.5",
        "forecast_in_15_minutes": "207", "forecast_in_30_minutes": "231",
    }
    assert _state(hass, "last_carbs").attributes["time"] == "2026-01-08T12:20:00+00:00"
    assert _state(hass, "last_carbs").attributes["notes"] == "Lunch"
    assert _state(hass, "last_insulin").attributes["time"] == "2026-01-08T12:25:00+00:00"
    assert _state(hass, "insulin_on_board").attributes["source"] == "fixture-pump"
    assert _state(hass, "carbs_on_board").attributes["source"] == "fixture-meal"
    assert hass.states.get("sensor.parity_site_current_basal") is None
    calendar = hass.states.get(CALENDAR)
    assert calendar.state == "off"
    assert calendar.attributes["message"] == "Meal Bolus: 45 g carbs"


async def _assert_calendar_range(hass, entry):
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": CALENDAR,
            "start_date_time": "2026-01-08T12:23:00+00:00",
            "end_date_time": "2026-01-08T13:00:00+00:00",
        },
        blocking=True,
        return_response=True,
    )
    assert [
        (event["summary"], event["start"], event["end"]) for event in response[CALENDAR]["events"]
    ] == [
        ("Carb Correction: 30 g carbs", "2026-01-08T12:20:00+00:00", "2026-01-08T12:25:00+00:00"),
        ("Correction Bolus: 0.5 U insulin", "2026-01-08T12:25:00+00:00", "2026-01-08T12:35:00+00:00"),
        ("Correction Bolus", "2026-01-08T12:35:00+00:00", "2026-01-08T12:40:00+00:00"),
    ]
    assert response[CALENDAR]["events"][0]["description"] == "Lunch\nEntered by Fixture"
    assert [treatment.uid for treatment in entry.runtime_data.data.treatments] == [
        "carbs-1", "insulin-1", "insulin-zero", "meal-next",
    ]


async def test_reload_uses_v3_with_stable_entities_and_calendar(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    """A server upgrade changes the wire paths while an existing HA entry keeps its identity."""
    freezer.move_to(AFTERNOON)
    _mock_site(aioclient_mock, reading=AFTERNOON_READING, forecast=AFTERNOON_FORECAST)
    entry = await _configure(hass)
    try:
        _assert_snapshot(hass, reading=AFTERNOON_READING)
        await _assert_calendar_range(hass, entry)
        _assert_wire_mode(aioclient_mock, v3=False, token=TOKEN, discovery=True)
        ids = _entity_ids(hass, entry)
        age_ids = {f"{URL}_{signal.key}" for signal in AGE_SIGNALS}
        assert age_ids <= ids.keys()
        assert ids[f"{URL}_insulin_age"] == "sensor.parity_site_insulin_age"
        assert {key: value for key, value in ids.items() if key not in age_ids} == {
            f"{URL}_{key}": f"sensor.parity_site_{name}"
            for key, name in (
                ("glucose", "glucose"), ("glucose_delta", "glucose_change"),
                ("glucose_trend", "glucose_trend"), ("last_reading", "last_reading"),
                ("iob", "insulin_on_board"), ("cob", "carbs_on_board"),
                ("last_carbs", "last_carbs"), ("last_insulin", "last_insulin"),
                ("basal", "current_basal"), ("forecast_15", "forecast_in_15_minutes"),
                ("forecast_30", "forecast_in_30_minutes"),
            )
        } | {
            f"{URL}_treatments": CALENDAR,
            f"{URL}_insulin_remaining": "sensor.parity_site_insulin_remaining",
        }
        device_id = _device_id(hass, entry)

        _mock_site(
            aioclient_mock, v3=True, reading=AFTERNOON_READING,
            v3_reading=AFTERNOON_READING, forecast=AFTERNOON_FORECAST,
        )
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is config_entries.ConfigEntryState.LOADED
        _assert_wire_mode(aioclient_mock, v3=True, token=TOKEN, discovery=True, bootstrap=True)
        _assert_snapshot(hass, reading=AFTERNOON_READING)
        aioclient_mock.mock_calls.clear()
        await _assert_calendar_range(hass, entry)
        calendar_calls = aioclient_mock.mock_calls
        assert {url.path for _, url, _, _ in calendar_calls} == {TREATMENTS_V3}
        assert {
            key for _, url, _, _ in calendar_calls for key in url.query if key.endswith("$gte")
        } == {"date$gte", "created_at$gte"}
        for _, url, _, headers in calendar_calls:
            headers = {key.lower(): value for key, value in headers.items()}
            assert headers["authorization"] == f"Bearer {BEARER}"
            assert "api-secret" not in headers and "token" not in url.query
        assert _entity_ids(hass, entry) == ids
        assert _device_id(hass, entry) == device_id

        aioclient_mock.mock_calls.clear()
        await _refresh(hass, entry)
        _assert_wire_mode(aioclient_mock, v3=True, token=TOKEN)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        # Flush writes scheduled by the reload before the HA fixture shuts down.
        hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await hass.async_block_till_done()


@pytest.mark.parametrize("v3", [False, True])
async def test_calendar_reads_authoritative_overlaps_and_preserves_cached_fallback(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, v3
):
    """An independently filtered calendar request can discover an interval the cache misses."""
    freezer.move_to(AFTERNOON)
    start = "2026-01-08T18:45:00+00:00"
    end = "2026-01-08T19:00:00+00:00"

    def treatment(uid, clock, notes, **fields):
        created = f"2026-01-08T{clock}+00:00"
        return {"_id": uid, "created_at": created, "eventType": "Note", "notes": notes, **fields}

    cached_long = treatment("long", "18:30:00", "Cached duration", duration=10)
    cached_marker = treatment("stale", "18:46:00", "Cached marker")
    default = treatment("default", "18:43:00", "Five-minute default")
    rows = [
        {**cached_long, "notes": "Authoritative interval", "duration": 20,
         "date": _ms(cached_long["created_at"])},
        treatment("ends-at-start", "18:35:00", "Ends exactly at the start", duration=10),
        default,
        treatment("within", "18:48:00", "First page only", date=_ms("2026-01-08T18:48:00+00:00")),
        treatment("starts-at-end", "19:00:00", "Starts exactly at the end",
                  date=_ms("2026-01-08T19:00:00+00:00")),
    ]
    future_dose = treatment("future-dose", "21:00:00", "Future", carbs=999, insulin=999)
    cached = (*LEGACY_TREATMENTS, cached_long, cached_marker, default, future_dose)
    _mock_site(aioclient_mock, v3=v3, treatments=cached)
    entry = await _configure(hass)
    failed_scan = False
    fault_sent = False

    async def treatment_response(method, url, data):
        nonlocal fault_sent
        query = url.query

        def reply(result, status=200):
            payload = {"status": status, "result": result} if v3 else result
            return AiohttpClientMockResponse(method, url, status=status, json=payload)

        if v3 and "duration$gt" in query:
            assert query["sort$desc"] == "duration" and query["limit"] == "1"
            assert query["skip"] == "0" and query["duration$gt"] == "5"
            candidates = [row for row in rows if row.get("duration", 0) > 5]
            return reply(sorted(candidates, key=lambda row: row["duration"], reverse=True)[:1])
        if not v3 and query.get("count") == "1":
            # The 15.0.6 Mongo driver does not coerce object sort directions.
            if "sort[duration]" in query:
                return AiohttpClientMockResponse(method, url, status=200, text="")
            # Nightscout 15.0.6 does not coerce v1 duration comparison operands.
            if "find[duration][$gt]" in query:
                return reply([])
            assert query["sort[0][0]"] == "duration" and query["sort[0][1]"] == "desc"
            assert query["find[duration][$type]"] == "number"
            assert query["find[created_at][$lt]"] == "2026-01-08T18:40:00.000Z"
            candidates = [
                row for row in rows if type(row.get("duration")) in (int, float)
                and _ms(row["created_at"]) < _ms(query["find[created_at][$lt]"])
            ]
            return reply(sorted(candidates, key=lambda row: row["duration"], reverse=True)[:1])

        if not v3:
            low, high = (_ms(query[f"find[created_at][${op}]"]) for op in ("gte", "lte"))
            count = int(query["count"])
            if failed_scan:
                fault_sent = True
                return reply([rows[0]] * count)
            return reply([row for row in rows if low <= _ms(row["created_at"]) <= high][:count])

        field = "date" if "date$gte" in query else "created_at"
        assert query["sort$desc"] == field
        inclusive_end = f"{field}$lte" in query
        end_predicate = "lte" if inclusive_end else "lt"
        low, high = _ms(query[f"{field}$gte"]), _ms(query[f"{field}${end_predicate}"])
        limit, skip = int(query["limit"]), int(query["skip"])
        if failed_scan:
            if limit > 1:
                return AiohttpClientMockResponse(method, url, status=400, json={
                    "status": 400, "message": "Parameter limit out of tolerance",
                })
            if skip:
                fault_sent = True
                return reply([], status=503)

        def stored_time(row):
            return row.get("date") if field == "date" else _ms(row["created_at"])

        eligible = sorted(
            (
                row for row in rows if (when := stored_time(row)) is not None
                and low <= when and (when < high or inclusive_end and when == high)
            ),
            key=stored_time, reverse=True,
        )
        return reply(eligible[skip:skip + limit])

    async def calendar(request_end=end):
        response = await hass.services.async_call(
            "calendar", "get_events",
            {"entity_id": CALENDAR, "start_date_time": start, "end_date_time": request_end},
            blocking=True, return_response=True,
        )
        return response[CALENDAR]["events"]

    try:
        # Cache really contains future intake, yet the earlier nonzero values remain current.
        assert any(row.uid == "future-dose" for row in entry.runtime_data.data.treatments)
        assert _state(hass, "last_carbs").state == "30"
        assert _state(hass, "last_insulin").state == "0.5"
        aioclient_mock.clear_requests()
        aioclient_mock.get(
            f"{URL}{TREATMENTS_V3 if v3 else TREATMENTS_V1}", side_effect=treatment_response,
        )

        events = await calendar()
        assert [(event["summary"], event["start"], event["end"]) for event in events] == [
            ("Authoritative interval", "2026-01-08T18:30:00+00:00", "2026-01-08T18:50:00+00:00"),
            ("Five-minute default", "2026-01-08T18:43:00+00:00", "2026-01-08T18:48:00+00:00"),
            ("First page only", "2026-01-08T18:48:00+00:00", "2026-01-08T18:53:00+00:00"),
        ]
        fractional_end = await calendar("2026-01-08T19:00:00.000001+00:00")
        assert fractional_end[-1]["summary"] == "Starts exactly at the end"

        rows[0] = {**rows[0], "notes": "Edited interval", "duration": 21}
        edited = await calendar()
        assert edited[0]["summary"] == "Edited interval"
        assert edited[0]["end"] == "2026-01-08T18:51:00+00:00"
        del rows[0]
        expected = ["Five-minute default", "First page only"]
        assert [event["summary"] for event in await calendar()] == expected
        for short_duration in (4, 5):
            rows[0]["duration"] = short_duration
            assert [event["summary"] for event in await calendar()] == expected
        del rows[0]["duration"]
        assert [event["summary"] for event in await calendar()] == expected

        failed_scan = True
        assert [event["summary"] for event in await calendar()] == ["Five-minute default", "Cached marker"]
        assert fault_sent  # v3 reached a later page; v1 filled the bounded result.
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_forecasts_keep_v2_anchor_and_update_each_horizon(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    """Use the v2 forecast anchor and keep the original curve after the scalars expire."""
    freezer.move_to(EARLY)
    _mock_site(aioclient_mock, v3=True, v3_reading=V3_READING, v3_glucose=162)
    entry = await _configure(hass)
    try:
        _assert_wire_mode(aioclient_mock, v3=True, token=TOKEN, discovery=True, bootstrap=True)
        paths = {url.path for _, url, _, _ in aioclient_mock.mock_calls}
        assert EXCHANGE in paths
        assert _state(hass, "glucose").state == "162"
        assert _state(hass, "last_reading").state == V3_READING
        assert _state(hass, "forecast_in_15_minutes").state == "207"
        forecast_30 = _state(hass, "forecast_in_30_minutes")
        assert forecast_30.state == "231"
        assert forecast_30.attributes["model"] == "AR2"
        assert forecast_30.attributes["forecast"] == [
            {"time": "2026-01-08T12:15:02+00:00", "mgdl": 207},
            {"time": "2026-01-08T12:30:02+00:00", "mgdl": 231},
        ]

        for points, expected in (
            (FORECAST[:1], ("207", STATE_UNKNOWN)),
            (FORECAST[1:], (STATE_UNKNOWN, "231")),
            (None, (STATE_UNKNOWN, STATE_UNKNOWN)),
        ):
            _mock_site(aioclient_mock, v3=True, v3_reading=V3_READING, v3_glucose=162, forecast=points)
            await _refresh(hass, entry)
            _assert_wire_mode(aioclient_mock, v3=True, token=TOKEN)
            assert _state(hass, "glucose").state == "162"
            assert _state(hass, "last_reading").state == V3_READING
            assert (
                _state(hass, "forecast_in_15_minutes").state,
                _state(hass, "forecast_in_30_minutes").state,
            ) == expected
        assert _state(hass, "forecast_in_30_minutes").attributes["forecast"] == []

        freezer.move_to(AFTERNOON)
        _mock_site(aioclient_mock, v3=True, v3_reading=V3_READING, v3_glucose=162)
        await _refresh(hass, entry)
        assert _state(hass, "glucose").state == STATE_UNKNOWN
        assert _state(hass, "last_reading").state == V3_READING
        for horizon, (target, _) in zip((15, 30), FORECAST, strict=True):
            expired = _state(hass, f"forecast_in_{horizon}_minutes")
            assert expired.state == STATE_UNKNOWN
            assert expired.attributes["source_time"] == READING
            assert expired.attributes["forecast_time"] == target
        assert _state(hass, "forecast_in_30_minutes").attributes["forecast"] == forecast_30.attributes["forecast"]
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("credential", "uses_secret"), [(None, False), (LEGACY_WIRE_INPUT, True)], ids=["public", "stored-api-secret"],
)
async def test_public_and_stored_secret_entries_keep_legacy_reads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, credential, uses_secret
):
    """Existing public and explicitly configured secret entries work without v3 discovery."""
    freezer.move_to(AFTERNOON)
    _mock_site(aioclient_mock, reading=AFTERNOON_READING, forecast=AFTERNOON_FORECAST)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=URL,
        title="Parity Site",
        data={CONF_URL: URL, CONF_API_KEY: credential, CONF_USE_API_SECRET: uses_secret},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        _assert_snapshot(hass, reading=AFTERNOON_READING)
        _assert_wire_mode(
            aioclient_mock, v3=False, secret=LEGACY_WIRE_SHA1 if uses_secret else None,
        )
        assert VERSION not in {url.path for _, url, _, _ in aioclient_mock.mock_calls}
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
