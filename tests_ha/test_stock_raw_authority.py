"""Exercise current glucose authority through successful legacy and v3 raw reads."""

import pytest
from homeassistant.const import STATE_UNKNOWN

from test_sgv_ingestion import (
    ENTRIES,
    LEGACY_ENTRIES,
    NOW,
    _configure,
    _current,
    _entity,
    _ms,
    _Nightscout,
    _unload,
)


@pytest.mark.parametrize("v3", [False, True], ids=["legacy", "v3"])
@pytest.mark.parametrize("v2_time", [None, "2026-01-08T12:09:00+00:00"], ids=["no-bgnow", "newer-bgnow"])
async def test_successful_raw_owns_current_without_matching_bgnow(hass, aioclient_mock, freezer, v3, v2_time):
    freezer.move_to(NOW)
    site = _Nightscout(aioclient_mock, name="RawAuthority", v3=v3)
    raw_time = "2026-01-08T12:07:00+00:00"
    raw_glucose = 162
    raw_stamp = site.put("raw-current", raw_time, raw_glucose, direction="FortyFiveUp")
    if v2_time is not None:
        site.bgnow = {"last": 167, "mills": _ms(v2_time)}
    forecasts = (
        ("forecast_15", "2026-01-08T12:24:02+00:00", 199),
        ("forecast_30", "2026-01-08T12:39:02+00:00", 204),
    )
    site.calculations = {
        "delta": {"mgdl": 12},
        "direction": {"value": "Flat"},
        "ar2": {"forecast": {"predicted": [
            {"mills": _ms(target), "mgdl": value} for _, target, value in forecasts
        ]}},
    }

    entry = await _configure(hass, site)
    try:
        raw_route, other_route = (ENTRIES, LEGACY_ENTRIES) if v3 else (LEGACY_ENTRIES, ENTRIES)
        # The helper records these batches only for successful raw responses.
        assert (raw_route, [(_ms(raw_time), raw_glucose, raw_stamp)]) in site.raw_batches
        assert all(path != other_route for path, _ in site.raw_batches)
        _current(hass, entry, "162", raw_time)
        glucose = hass.states.get(_entity(hass, entry))
        assert glucose.attributes["direction"] == "FortyFiveUp"
        assert glucose.attributes.get("delta") is None
        assert hass.states.get(_entity(hass, entry, "glucose_delta")).state == STATE_UNKNOWN

        for key, target, value in forecasts:
            forecast = hass.states.get(_entity(hass, entry, key))
            assert forecast.attributes["source_time"] == v2_time
            if v2_time is None:
                assert forecast.state == STATE_UNKNOWN
                assert forecast.attributes["forecast_time"] is None
            else:
                assert forecast.state == str(value)
                assert forecast.attributes["forecast_time"] == target
    finally:
        await _unload(hass, entry)
