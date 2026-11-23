"""Test Home Assistant alarm options and refreshes with synthetic site settings."""

from copy import deepcopy
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_URL
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nightscout_extended import coordinator as coordinator_module
from custom_components.nightscout_extended.api import NightscoutAuthError, NightscoutConnectionError
from custom_components.nightscout_extended.const import CONF_SCAN_INTERVAL, DOMAIN

URL = "https://ns.example.com"
OVERRIDES = {
    "alarm_urgent_low_mgdl": 45,
    "alarm_low_mgdl": 75,
    "alarm_high_mgdl": 210.5,
    "alarm_urgent_high_mgdl": 450,
}
STATUS = {
    "settings": {
        "units": "mmol",
        "thresholds": {"bgLow": 50, "bgTargetBottom": 80, "bgTargetTop": 190, "bgHigh": 250},
        "alarmUrgentLow": True,
        "alarmLow": False,
        "alarmHigh": True,
        "alarmUrgentHigh": True,
    }
}
SITE_THRESHOLDS = {"urgent_low": 50, "low": 80, "high": 190, "urgent_high": 250}


async def _setup(hass, aioclient_mock, status=STATUS, options=None):
    aioclient_mock.get(f"{URL}/api/v1/status.json", json=status)
    aioclient_mock.get(f"{URL}/api/v2/properties/bgnow", json={})
    aioclient_mock.get(
        f"{URL}/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal", json={}
    )
    aioclient_mock.get(f"{URL}/api/v1/treatments.json", json=[])
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL}, options=options or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _form(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    return result


def _field(result, name):
    return next(field for field in result["data_schema"].schema if field.schema == name)


async def test_options_override_independently_and_clear(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, options={"unrelated": True, CONF_SCAN_INTERVAL: 90})
    coordinator = entry.runtime_data
    assert coordinator.alarm_settings == {
        "units": "mmol",
        "thresholds_mgdl": SITE_THRESHOLDS,
        "enabled": {"urgent_low": True, "low": False, "high": True, "urgent_high": True},
    }

    result = await _form(hass, entry)
    for key in OVERRIDES:
        assert not _field(result, key).description
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 90, **OVERRIDES}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {"unrelated": True, CONF_SCAN_INTERVAL: 90, **OVERRIDES}
    coordinator = entry.runtime_data
    assert coordinator.update_interval.total_seconds() == 90
    assert coordinator.alarm_settings["thresholds_mgdl"] == {
        "urgent_low": 45, "low": 75, "high": 210.5, "urgent_high": 450
    }
    assert coordinator.alarm_settings["enabled"]["low"] is False
    assert coordinator.thresholds_mgdl == SITE_THRESHOLDS

    result = await _form(hass, entry)
    for key, value in OVERRIDES.items():
        assert _field(result, key).description["suggested_value"] == value
    update = {CONF_SCAN_INTERVAL: 90, **OVERRIDES}
    del update["alarm_urgent_high_mgdl"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], update)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert "alarm_urgent_high_mgdl" not in entry.options
    assert entry.runtime_data.alarm_settings["thresholds_mgdl"] == {
        "urgent_low": 45, "low": 75, "high": 210.5, "urgent_high": 250
    }


async def test_options_validate_effective_order(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock)
    form = await _form(hass, entry)
    for threshold in (190, 191):
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {CONF_SCAN_INTERVAL: 60, "alarm_low_mgdl": threshold}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "alarm_threshold_order"}
        assert not entry.options

    # Nightscout's two displayed thresholds can match after unit rounding.
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {CONF_SCAN_INTERVAL: 60, "alarm_high_mgdl": 190, "alarm_urgent_high_mgdl": 190.1},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.alarm_settings["thresholds_mgdl"]["urgent_high"] == 190.1


@pytest.mark.parametrize(
    "metadata",
    [
        {"units": "mmol", "thresholds": STATUS["settings"]["thresholds"]},
        {**STATUS["settings"], "alarmLow": "false"},
        {**STATUS["settings"], "units": "unknown"},
        {**STATUS["settings"], "thresholds": []},
        {**STATUS["settings"], "thresholds": {**STATUS["settings"]["thresholds"], "bgLow": True}},
    ],
)
async def test_missing_or_invalid_metadata_does_not_supply_alarm_settings(
    hass, aioclient_mock, metadata
):
    entry = await _setup(hass, aioclient_mock, status={"settings": metadata})
    assert entry.runtime_data.alarm_settings is None
    assert entry.runtime_data.thresholds_mgdl["urgent_high"] in (250, 260)


@pytest.mark.parametrize("error", [NightscoutConnectionError, NightscoutAuthError])
async def test_refresh_site_changes_failure_and_recovery(hass, aioclient_mock, monkeypatch, error):
    now = 0
    monkeypatch.setattr(coordinator_module, "monotonic", lambda: now)
    entry = await _setup(
        hass, aioclient_mock, options={"alarm_urgent_high_mgdl": 300}
    )
    coordinator = entry.runtime_data

    changed = deepcopy(STATUS)
    changed["settings"]["units"] = "mg/dl"
    changed["settings"]["alarmHigh"] = False
    changed["settings"]["thresholds"]["bgTargetTop"] = 200
    changed["settings"]["thresholds"]["bgHigh"] = 280
    conflicting = deepcopy(changed)
    conflicting["settings"]["thresholds"]["bgTargetTop"] = 320
    conflicting["settings"]["thresholds"]["bgHigh"] = 360

    async def fail_status():
        coordinator.client.set_uses_api_secret(True)
        raise error("Synthetic status failure")

    with patch.object(
        coordinator.client, "async_get_status", side_effect=[changed, conflicting, changed]
    ) as status:
        now += 299
        await coordinator.async_refresh()
        assert status.await_count == 0
        now += 1
        await coordinator.async_refresh()
        assert status.await_count == 1
        assert coordinator.alarm_settings["units"] == "mg/dl"
        assert coordinator.alarm_settings["enabled"]["high"] is False
        assert coordinator.alarm_settings["thresholds_mgdl"]["high"] == 200
        assert coordinator.alarm_settings["thresholds_mgdl"]["urgent_high"] == 300
        assert coordinator.thresholds_mgdl["urgent_high"] == 280

        now += 300
        await coordinator.async_refresh()
        assert coordinator.alarm_settings is None
        assert coordinator.thresholds_mgdl["urgent_high"] == 360

        now += 300
        await coordinator.async_refresh()
        assert status.await_count == 3

    with patch.object(coordinator.client, "async_get_status", side_effect=fail_status) as status:
        now += 300
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert coordinator.alarm_settings is None
        assert coordinator.thresholds_mgdl["urgent_high"] == 280
        assert not coordinator.client.uses_api_secret
        now += 60
        await coordinator.async_refresh()
        assert status.await_count == 1
        assert coordinator.alarm_settings is None

    with patch.object(coordinator.client, "async_get_status", return_value=changed):
        now += 240
        await coordinator.async_refresh()
    assert coordinator.alarm_settings["thresholds_mgdl"]["urgent_high"] == 300
