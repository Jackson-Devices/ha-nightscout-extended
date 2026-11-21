"""Exercise the insulin remaining entity through actual Home Assistant entry setup."""

import json

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.nightscout_extended.const import (
    CONF_RESERVOIR_UPLOADER, CONF_SCAN_INTERVAL, CONF_SGV_HISTORY_DAYS, DOMAIN,
)
from custom_components.nightscout_extended.diagnostics import async_get_config_entry_diagnostics
from test_v3_integration import CALENDAR, URL, _configure, _mock_site, _refresh, _state

DEVICE_A = "openaps://Fixture A"
DEVICE_B = "openaps://Fixture B"


def _status(identifier, device, units, contact="2026-01-08T12:00:00Z"):
    return {
        "identifier": identifier,
        "device": device,
        "pump": {"reservoir": units, "status": {"timestamp": contact}, "clock": "2026-01-08T12:01:00Z"},
    }


@pytest.mark.parametrize("v3", [False, True], ids=["v1", "v3"])
async def test_reservoir_unavailable_never_takes_down_glucose_or_calendar(hass, aioclient_mock, freezer, v3):
    freezer.move_to("2026-01-08T12:01:00+00:00")
    _mock_site(aioclient_mock, v3=v3, reservoir=[_status("a", DEVICE_A, 0), _status("b", DEVICE_B, 45)])
    entry = await _configure(hass)
    try:
        assert _state(hass, "insulin_remaining").state == STATE_UNAVAILABLE
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        assert flow["type"] is FlowResultType.FORM
        configured = await hass.config_entries.options.async_configure(
            flow["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_SGV_HISTORY_DAYS: 2, CONF_RESERVOIR_UPLOADER: DEVICE_A},
        )
        assert configured["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_SGV_HISTORY_DAYS] == 2
        await hass.async_block_till_done()

        reading = _state(hass, "insulin_remaining")
        assert reading.state == "0"
        assert reading.attributes["unit_of_measurement"] == "U"
        assert reading.attributes["source_time"] == "2026-01-08T12:00:00+00:00"
        assert reading.attributes["uploader"] == DEVICE_A
        expected_id = f"{URL}_insulin_remaining"
        assert er.async_get(hass).async_get(reading.entity_id).unique_id == expected_id
        assert DEVICE_A not in json.dumps(await async_get_config_entry_diagnostics(hass, entry))

        for rows, status in (
            ([_status("b", DEVICE_B, 45)], 200),
            ([_status("a", DEVICE_A, -1), _status("older", DEVICE_A, 12, "2026-01-08T11:50:00Z")], 200),
            ([_status("a", DEVICE_A, 3), _status("conflict", DEVICE_A, 2)], 200),
            ([_status("future", DEVICE_A, 4, "2026-01-08T12:02:00Z")], 200),
            ([_status("stale", DEVICE_A, 3, "2026-01-08T11:00:59.999Z")], 200),
            ([], 403),
        ):
            _mock_site(aioclient_mock, v3=v3, reservoir=rows, reservoir_status=status)
            await _refresh(hass, entry)
            reading = _state(hass, "insulin_remaining")
            assert reading.state == STATE_UNAVAILABLE
            assert "source_time" not in reading.attributes and "uploader" not in reading.attributes
            assert _state(hass, "glucose").state == "153"
            assert hass.states.get(CALENDAR).state != STATE_UNAVAILABLE

        result = await hass.services.async_call(
            "calendar", "get_events",
            {"entity_id": CALENDAR, "start_date_time": "2026-01-08T12:00:00+00:00",
             "end_date_time": "2026-01-08T12:30:00+00:00"},
            blocking=True, return_response=True,
        )
        assert len(result[CALENDAR]["events"]) == 2
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_discovered_uploader_is_saved_before_state_and_restored_on_entry_restart(
    hass, aioclient_mock, freezer, hass_storage,
):
    freezer.move_to("2026-01-08T12:01:00+00:00")
    _mock_site(aioclient_mock, reservoir=[_status("a", DEVICE_A, 8.25)])
    entry = await _configure(hass)
    key = f"{DOMAIN}.reservoir.{entry.entry_id}"
    try:
        assert _state(hass, "insulin_remaining").state == "8.25"
        assert hass_storage[key]["data"] == {"device": DEVICE_A}
        _mock_site(aioclient_mock, reservoir=[_status("b", DEVICE_B, 30)])
        await _refresh(hass, entry)
        assert _state(hass, "insulin_remaining").state == STATE_UNAVAILABLE

        coordinator = entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not coordinator
        assert _state(hass, "insulin_remaining").state == STATE_UNAVAILABLE
        assert hass_storage[key]["data"] == {"device": DEVICE_A}

        _mock_site(aioclient_mock, reservoir=[_status("a-new", DEVICE_A, 0), _status("b", DEVICE_B, 30)])
        await _refresh(hass, entry)
        assert _state(hass, "insulin_remaining").state == "0"

        assert await hass.config_entries.async_remove(entry.entry_id)
        assert key not in hass_storage
    finally:
        if hass.config_entries.async_get_entry(entry.entry_id) is not None:
            await hass.config_entries.async_unload(entry.entry_id)
