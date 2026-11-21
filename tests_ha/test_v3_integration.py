"""Exercise Nightscout protocol selection through real Home Assistant entry setup."""

from datetime import datetime
from hashlib import sha1
import re
from time import time

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL, EVENT_HOMEASSISTANT_FINAL_WRITE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.nightscout_extended.const import CONF_USE_API_SECRET, DOMAIN

URL = "https://parity.example.invalid"
TOKEN = "fixture-subject"
BEARER = "fixture-bearer"
SECRET = "fixture-site-secret"
VERSION = "/api/v3/version"
EXCHANGE = f"/api/v2/authorization/request/{TOKEN}"
ENTRIES_V3 = "/api/v3/entries"
TREATMENTS_V3 = "/api/v3/treatments"
TREATMENTS_V1 = "/api/v1/treatments.json"
PROPERTIES = "/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal"
READING = "2026-01-08T12:00:00+00:00"
V3_READING = "2026-01-08T12:07:00+00:00"
EARLY = "2026-01-08T12:10:00+00:00"
AFTERNOON = "2026-01-08T17:00:00+00:00"
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


def _mock_site(
    mocker: AiohttpClientMocker, *, v3=False, v3_reading=READING, v3_glucose=153,
    forecast=FORECAST,
):
    """Install responses at the HTTP boundary of HA's existing aiohttp fixture."""
    mocker.clear_requests()
    mocker.get(
        f"{URL}/api/v1/status.json",
        json={"version": "15.0.9", "settings": {"customTitle": "Parity Site", "units": "mg/dl"}},
    )
    bgnow = {"last": 153, "mills": _ms(READING)}
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
    if not v3:
        mocker.get(f"{URL}{VERSION}", status=404)
        mocker.get(f"{URL}{TREATMENTS_V1}", json=list(LEGACY_TREATMENTS))
        return

    now = int(time())
    mocker.get(
        f"{URL}{VERSION}",
        json={"status": 200, "result": {"version": "15.0.9", "apiVersion": "3.0.5"}},
    )
    mocker.get(f"{URL}{EXCHANGE}", json={"token": BEARER, "iat": now, "exp": now + 86400})
    mocker.get(
        f"{URL}{ENTRIES_V3}",
        json={"status": 200, "result": [{"type": "sgv", "sgv": v3_glucose, "date": _ms(v3_reading)}]},
    )
    modern = []
    for row in reversed(LEGACY_TREATMENTS[1:]):
        treatment = dict(row)
        treatment["identifier"] = treatment.pop("_id")
        treatment["date"] = _ms(treatment.pop("created_at"))
        modern.append(treatment)
    path = re.escape(URL + TREATMENTS_V3)
    for field, results in (
        ("date", modern),
        ("created_at", [LEGACY_TREATMENTS[0]]),
    ):
        query = re.compile(rf"^{path}\?(?:[^#]*&)?{field}(?:\$|%24)gte=")
        mocker.get(query, json={"status": 200, "result": results})


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


def _assert_wire_mode(mocker, *, v3, token=None, secret=None, discovery=False):
    calls = mocker.mock_calls
    paths = {url.path for _, url, _, _ in calls}
    assert PROPERTIES in paths
    if discovery:
        assert VERSION in paths
    if v3:
        assert {ENTRIES_V3, TREATMENTS_V3} <= paths
        assert TREATMENTS_V1 not in paths
    else:
        assert not paths.intersection({ENTRIES_V3, TREATMENTS_V3, EXCHANGE})
        assert TREATMENTS_V1 in paths
    for method, url, _, headers in calls:
        assert method.upper() == "GET"
        assert url.host == "parity.example.invalid"
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        if url.path in {ENTRIES_V3, TREATMENTS_V3}:
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


def _assert_snapshot(hass):
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
        "last_reading": READING, "insulin_on_board": "1.35", "carbs_on_board": "28.9",
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
    _mock_site(aioclient_mock)
    entry = await _configure(hass)
    try:
        _assert_snapshot(hass)
        await _assert_calendar_range(hass, entry)
        _assert_wire_mode(aioclient_mock, v3=False, token=TOKEN, discovery=True)
        ids = _entity_ids(hass, entry)
        assert ids == {
            f"{URL}_{key}": f"sensor.parity_site_{name}"
            for key, name in (
                ("glucose", "glucose"), ("glucose_delta", "glucose_change"),
                ("glucose_trend", "glucose_trend"), ("last_reading", "last_reading"),
                ("iob", "insulin_on_board"), ("cob", "carbs_on_board"),
                ("last_carbs", "last_carbs"), ("last_insulin", "last_insulin"),
                ("basal", "current_basal"), ("forecast_15", "forecast_in_15_minutes"),
                ("forecast_30", "forecast_in_30_minutes"),
            )
        } | {f"{URL}_treatments": CALENDAR}
        device_id = _device_id(hass, entry)

        _mock_site(aioclient_mock, v3=True)
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is config_entries.ConfigEntryState.LOADED
        _assert_snapshot(hass)
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


async def test_forecasts_keep_v2_anchor_and_update_each_horizon(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    """V3 SGV and wall-clock age do not change the v2 forecast anchor or mask missing data."""
    freezer.move_to(EARLY)
    _mock_site(aioclient_mock, v3=True, v3_reading=V3_READING, v3_glucose=162)
    entry = await _configure(hass)
    try:
        _assert_wire_mode(aioclient_mock, v3=True, token=TOKEN, discovery=True)
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

        freezer.move_to(AFTERNOON)
        await _refresh(hass, entry)
        assert _state(hass, "forecast_in_15_minutes").state == "207"
        assert _state(hass, "forecast_in_30_minutes").state == "231"

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
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("credential", "uses_secret"), [(None, False), (SECRET, True)], ids=["public", "stored-api-secret"],
)
async def test_public_and_stored_secret_entries_keep_legacy_reads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, credential, uses_secret
):
    """Existing public and explicitly configured secret entries work without v3 discovery."""
    freezer.move_to(AFTERNOON)
    _mock_site(aioclient_mock)
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
        _assert_snapshot(hass)
        _assert_wire_mode(
            aioclient_mock, v3=False, secret=sha1(SECRET.encode()).hexdigest() if uses_secret else None,
        )
        assert VERSION not in {url.path for _, url, _, _ in aioclient_mock.mock_calls}
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
