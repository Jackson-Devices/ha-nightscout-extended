"""Exercise the Nightscout alarm on the live HA glucose entity."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_URL, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import async_get_platforms
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.nightscout_extended.api import PROPERTIES, parse_properties
from custom_components.nightscout_extended.const import CONF_SCAN_INTERVAL, DOMAIN
from custom_components.nightscout_extended import sensor

URL = "https://ns.example.com"
GLUCOSE = "sensor.ns_glucose"
BASE = datetime(2026, 1, 1, 12, tzinfo=UTC)
SITE_THRESHOLDS = {"bgLow": 55, "bgTargetBottom": 80, "bgTargetTop": 180, "bgHigh": 250}
SITE_ALARMS = {"alarmUrgentLow": True, "alarmLow": True, "alarmHigh": True, "alarmUrgentHigh": True}


def _row(mgdl, seconds=0):
    source = BASE + timedelta(seconds=seconds)
    return {"mgdl": mgdl, "mills": int(source.timestamp() * 1000)}


def _properties(mgdl, seconds=0, *, raw=None):
    reading = _row(mgdl, seconds)
    return {
        "bgnow": {
            "last": mgdl,
            "mills": reading["mills"],
            "sgvs": [reading] if raw is None else raw,
        },
        "delta": {"mgdl": 12},
        "direction": {"value": "FortyFiveUp"},
    }


async def _setup(
    hass: HomeAssistant, aioclient_mock, props, *, units="mg/dl", site_alarms=None,
):
    status = {
        "version": "15.0.6",
        "settings": {
            "units": units,
            "thresholds": SITE_THRESHOLDS,
            **(SITE_ALARMS if site_alarms is None else site_alarms),
        },
    }
    aioclient_mock.get(f"{URL}/api/v1/status.json", json=status)
    aioclient_mock.get(f"{URL}/api/v2/properties/bgnow", json=props)
    aioclient_mock.get(f"{URL}/api/v2/properties/{PROPERTIES}", json=props)
    aioclient_mock.get(f"{URL}/api/v1/treatments.json", json=[])

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=URL, title="NS",
        data={CONF_URL: URL}, options={CONF_SCAN_INTERVAL: 3600},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(
    ("units", "reading", "disabled", "expected"),
    [
        ("mg/dl", 401, None, "urgent_high"),
        ("mg/dl", 250, None, "high"),
        ("mg/dl", 180, None, "none"),
        ("mg/dl", 55, None, "low"),
        ("mg/dl", 54, None, "urgent_low"),
        ("mg/dl", 80, None, "none"),
        ("mg/dl", 401, "alarmUrgentHigh", "high"),
        ("mg/dl", 54, "alarmUrgentLow", "low"),
        ("mmol", 181, None, "none"),
        ("mmol", 182, None, "high"),
    ],
)
async def test_simple_alarm_uses_stock_rules(hass, aioclient_mock, freezer, units, reading, disabled, expected):
    freezer.move_to(BASE)
    flags = {**SITE_ALARMS, **({disabled: False} if disabled else {})}
    entry = await _setup(hass, aioclient_mock, _properties(reading), units=units, site_alarms=flags)

    state = hass.states.get(GLUCOSE)
    assert state.attributes["simple_alarm"] == expected
    assert state.attributes["simple_alarm_until"] == "2026-01-01T12:10:00+00:00"
    assert state.attributes["native_mgdl"] == reading
    assert state.attributes["source_time"] == BASE.isoformat()
    assert state.attributes["delta"] == 12
    assert state.attributes["direction"] == "FortyFiveUp"
    assert state.attributes["thresholds_mgdl"] == state.attributes["alarm_thresholds_mgdl"] == {
        "urgent_low": 55, "low": 80, "high": 180, "urgent_high": 250,
    }
    if units == "mg/dl":
        assert state.state == str(reading)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("raw", "elapsed", "site_alarms", "expected", "state", "error_code"),
    [
        ([_row(401), _row(39)], 1, None, "urgent_high", "401", None),
        ([_row(401), _row(39, -1)], 1, None, "urgent_high", "401", None),
        ([_row(39, 3), _row(401)], 2, None, "urgent_high", "401", None),
        ([_row(401, 3)], 2, None, None, "401", None),
        # Nightscout's display shows the newest raw entry: a newer CGM error code 38 shows as "38??" and is not
        # a glucose value, so the older 401 must not stay on screen (lib/client/index.js updateCurrentSGV).
        ([_row(38, 1), _row(401)], 2, None, None, "unknown", 38),
        ([_row(420)], 1, None, None, "401", None),
        ([_row(401, 1)], 2, None, None, "401", None),
        ([], 0, None, None, "401", None),
        ([{"mgdl": 39}, _row(401)], 0, None, None, "401", None),
        ([_row(401)], 600, None, None, "401", None),
        ([_row(401)], 0, {}, None, "401", None),
    ],
)
async def test_alarm_uses_latest_raw_sgv_and_fails_closed(
    hass, aioclient_mock, freezer, raw, elapsed, site_alarms, expected, state, error_code,
):
    freezer.move_to(BASE + timedelta(seconds=elapsed))
    entry = await _setup(hass, aioclient_mock, _properties(401, raw=raw), site_alarms=site_alarms)

    glucose = hass.states.get(GLUCOSE)
    assert glucose.state == state
    assert glucose.attributes.get("cgm_error_code") == error_code
    assert glucose.attributes["simple_alarm"] == expected
    if expected is None:
        assert glucose.attributes["simple_alarm_until"] is None
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("prior_reading", "raw_time_seconds"), [(None, 0), (155, 120), (401, 120), (401, 0)],
)
async def test_stock_39_uses_current_numeric_and_preserves_forecast_anchor(
    hass, aioclient_mock, freezer, prior_reading, raw_time_seconds,
):
    source_time = BASE + timedelta(seconds=raw_time_seconds)
    freezer.move_to(source_time + timedelta(seconds=1))
    current = {**_row(39, raw_time_seconds), "direction": "Flat"}
    if prior_reading is None:
        properties = {"bgnow": {"sgvs": [current]}}
    else:
        properties = _properties(prior_reading, raw=[current, _row(prior_reading)])
        properties["ar2"] = {"forecast": {"predicted": [_row(170, 902), _row(180, 1802)]}}
    entry = await _setup(hass, aioclient_mock, properties)

    state = hass.states.get(GLUCOSE)
    assert state.state == "39"
    assert state.attributes["native_mgdl"] == 39
    assert state.attributes["source_time"] == source_time.isoformat()
    assert hass.states.get("sensor.ns_last_reading").state == source_time.isoformat()
    assert state.attributes["direction"] == "Flat"
    assert state.attributes["delta"] is None
    assert state.attributes["simple_alarm"] is None
    assert state.attributes["simple_alarm_until"] is None
    if prior_reading is not None:
        assert entry.runtime_data.data.forecast_source_time == BASE
        assert hass.states.get("sensor.ns_forecast_in_15_minutes").state == "170"
        forecast = hass.states.get("sensor.ns_forecast_in_30_minutes")
        assert forecast.state == "180"
        assert forecast.attributes["forecast"][0]["time"] == (BASE + timedelta(seconds=902)).isoformat()
    else:
        assert entry.runtime_data.data.forecast_source_time is None
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(("reading", "expected"), [(401, "urgent_high"), (120, "none")])
async def test_simple_alarm_expires_without_polling(hass, aioclient_mock, freezer, reading, expected):
    freezer.move_to(BASE)
    entry = await _setup(hass, aioclient_mock, _properties(reading))
    before_calls = len(aioclient_mock.mock_calls)

    before_expiry = BASE + timedelta(minutes=9, seconds=59)
    freezer.move_to(before_expiry)
    async_fire_time_changed(hass, before_expiry)
    await hass.async_block_till_done()
    attrs = hass.states.get(GLUCOSE).attributes
    assert attrs["simple_alarm"] == expected
    assert attrs["simple_alarm_until"] == (BASE + timedelta(minutes=10)).isoformat()

    expiry = BASE + timedelta(minutes=10)
    freezer.move_to(expiry)
    async_fire_time_changed(hass, expiry)
    await hass.async_block_till_done()
    state = hass.states.get(GLUCOSE)
    assert state.state == str(reading)
    assert state.attributes["simple_alarm"] is None
    assert state.attributes["simple_alarm_until"] is None
    assert len(aioclient_mock.mock_calls) == before_calls
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_alarm_expiry_ignores_replaced_and_unloaded_callbacks(hass, aioclient_mock, freezer, monkeypatch):
    handlers = []
    original_tracker = sensor.async_track_point_in_utc_time

    def track(hass, action, when):
        handlers.append(action)
        return original_tracker(hass, action, when)

    monkeypatch.setattr(sensor, "async_track_point_in_utc_time", track)
    freezer.move_to(BASE)
    entry = await _setup(hass, aioclient_mock, _properties(401))
    assert len(handlers) == 1

    newer = BASE + timedelta(minutes=4)
    freezer.move_to(newer)
    entry.runtime_data.async_set_updated_data(parse_properties(_properties(60, seconds=240), now=newer))
    await hass.async_block_till_done()
    assert len(handlers) == 2
    assert hass.states.get(GLUCOSE).attributes["simple_alarm"] == "low"

    old_expiry = BASE + timedelta(minutes=10)
    freezer.move_to(old_expiry)
    with patch.object(sensor.NightscoutSensor, "async_write_ha_state") as write:
        handlers[0](old_expiry)
        write.assert_not_called()
    assert hass.states.get(GLUCOSE).attributes["simple_alarm"] == "low"

    assert await hass.config_entries.async_unload(entry.entry_id)
    unloaded_state = hass.states.get(GLUCOSE)
    assert unloaded_state is None or unloaded_state.state == STATE_UNAVAILABLE
    assert all(GLUCOSE not in platform.entities for platform in async_get_platforms(hass, DOMAIN))

    new_expiry = newer + timedelta(minutes=10)
    freezer.move_to(new_expiry)
    with patch.object(sensor.NightscoutSensor, "async_write_ha_state") as write:
        handlers[1](new_expiry)
        write.assert_not_called()
    assert hass.states.get(GLUCOSE) is unloaded_state
