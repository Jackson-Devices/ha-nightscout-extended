"""Exercise pump battery age through the Nightscout HTTP and HA sensor boundaries."""

from datetime import datetime
from time import time

import pytest
from homeassistant.const import (
    CONF_API_KEY,
    CONF_URL,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTime,
)
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.nightscout_extended.const import CONF_USE_API_SECRET, DOMAIN

URL = "https://pump-battery-age.example.invalid"
EVENT = "Pump Battery Change"
NOW = "2026-01-08T12:00:00+00:00"
OLD = "2026-01-05T06:30:00+00:00"
RECENT = "2026-01-08T10:00:00+00:00"
FUTURE = "2026-01-09T12:00:00+00:00"


def _ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


@pytest.fixture(params=["legacy", "v3"])
def battery_site(request, aioclient_mock, freezer):
    """Serve age lookups separately from calendar history in each API mode."""
    freezer.move_to(NOW)
    v3 = request.param == "v3"
    site = {
        "markers": ["2026-01-05T07:30:00+01:00"],
        "marker_status": 200,
        "upload": {"device": "old-uploader", "created_at": NOW, "pump": {"battery": {"percent": 12}}},
    }

    async def treatments(method, url, _data):
        if v3 and "eventType$in" in url.query:
            types = url.query["eventType$in"].split("|")
        else:
            types = [value for key, value in url.query.items() if key.startswith("find[eventType][$in][")]
        # The old marker is absent from the regular refresh and calendar window.
        # Return a future row when asked so the client must validate its event time.
        age_query = EVENT in types
        if age_query:
            assert not any("$gte" in key for key in url.query)
        timestamps = site["markers"] if age_query else []
        rows = [
            {"identifier": str(index), "date": _ms(at), "eventType": EVENT}
            if v3 else {"_id": str(index), "created_at": at, "eventType": EVENT}
            for index, at in enumerate(timestamps)
        ]
        status = site["marker_status"] if age_query else 200
        return AiohttpClientMockResponse(
            method, url, status=status, json={"status": status, "result": rows} if v3 else rows,
        )

    async def device_status(method, url, _data):
        upload = dict(site["upload"])
        if v3:
            upload["date"] = _ms(upload["created_at"])
        rows = [upload]
        return AiohttpClientMockResponse(
            method, url, status=200, json={"status": 200, "result": rows} if v3 else rows,
        )

    aioclient_mock.get(f"{URL}/api/v1/status.json", json={"settings": {"units": "mg/dl"}})
    properties = {"bgnow": {"last": 153, "mills": _ms(NOW)}}
    aioclient_mock.get(f"{URL}/api/v2/properties/bgnow", json=properties)
    aioclient_mock.get(f"{URL}/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal", json=properties)
    if v3:
        aioclient_mock.get(f"{URL}/api/v3/version", json={"status": 200, "result": {"apiVersion": "3.0.5"}})
        aioclient_mock.get(
            f"{URL}/api/v2/authorization/request/synthetic-subject",
            json={"token": "synthetic-bearer", "iat": int(time()), "exp": int(time()) + 86400},
        )
        aioclient_mock.get(
            f"{URL}/api/v3/entries",
            json={"status": 200, "result": [{"type": "sgv", "sgv": 153, "date": _ms(NOW)}]},
        )
        aioclient_mock.get(f"{URL}/api/v3/treatments", side_effect=treatments)
        aioclient_mock.get(f"{URL}/api/v3/devicestatus", side_effect=device_status)
    else:
        aioclient_mock.get(f"{URL}/api/v3/version", status=404)
        aioclient_mock.get(f"{URL}/api/v1/treatments.json", side_effect=treatments)
        aioclient_mock.get(f"{URL}/api/v1/devicestatus.json", side_effect=device_status)
    return site


async def _setup(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Battery Site", unique_id=URL,
        data={CONF_URL: URL, CONF_API_KEY: "synthetic-subject", CONF_USE_API_SECRET: False},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{URL}_pump_battery_age")
    assert entity_id is not None
    return entry, entity_id


async def _refresh(hass, entry):
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.last_update_success


async def test_pump_battery_age_uses_old_marker_despite_new_upload(hass, freezer, battery_site):
    entry, entity_id = await _setup(hass)
    try:
        state = hass.states.get(entity_id)
        assert float(state.state) == 77.5
        assert state.attributes["unit_of_measurement"] == UnitOfTime.HOURS
        assert state.attributes["source_time"] == OLD
        assert state.attributes["event_type"] == EVENT

        freezer.move_to("2026-01-08T13:00:00+00:00")
        battery_site["upload"] = {
            "device": "replacement-uploader",
            "created_at": "2026-01-08T12:59:00+00:00",
            "pump": {"battery": {"percent": 100}},
        }
        await _refresh(hass, entry)
        state = hass.states.get(entity_id)
        assert float(state.state) == 78.5
        assert state.attributes["source_time"] == OLD
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pump_battery_age_clears_missing_sources_and_recovers(hass, battery_site):
    entry, entity_id = await _setup(hass)
    try:
        assert hass.states.get(entity_id).attributes["source_time"] == OLD

        battery_site["markers"] = []
        await _refresh(hass, entry)
        state = hass.states.get(entity_id)
        assert state.state == STATE_UNKNOWN
        assert state.attributes.get("source_time") is None

        battery_site["markers"] = [FUTURE]
        await _refresh(hass, entry)
        state = hass.states.get(entity_id)
        assert state.state == STATE_UNKNOWN
        assert state.attributes.get("source_time") is None

        battery_site["markers"] = [RECENT]
        await _refresh(hass, entry)
        state = hass.states.get(entity_id)
        assert float(state.state) == 2
        assert state.attributes["source_time"] == RECENT

        battery_site["marker_status"] = 503
        await _refresh(hass, entry)
        state = hass.states.get(entity_id)
        assert state.state == STATE_UNAVAILABLE
        assert state.attributes.get("source_time") is None

        battery_site["marker_status"] = 200
        battery_site["markers"] = [OLD]
        await _refresh(hass, entry)
        state = hass.states.get(entity_id)
        assert float(state.state) == 77.5
        assert state.attributes["source_time"] == OLD
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
