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


@pytest.mark.parametrize("v3", [False, True], ids=["legacy", "v3"])
@pytest.mark.parametrize(
    "raw_glucose, v2_direction, raw_direction, expected_delta, expected_trend",
    [
        pytest.param(162.04, "Flat", "SingleDown", None, "single_down", id="same-time-different-value"),
        pytest.param(162, "Flat", "SingleDown", 7, "single_down", id="same-reading-conflicting-direction"),
        pytest.param(162, None, "FortyFiveUp", 7, "forty_five_up", id="same-reading-v2-omits-direction"),
        pytest.param(162, "Flat", None, 7, STATE_UNKNOWN, id="same-reading-raw-omits-direction"),
    ],
)
async def test_raw_metadata_requires_its_own_reading(
    hass, aioclient_mock, freezer, v3, raw_glucose, v2_direction, raw_direction, expected_delta,
    expected_trend,
):
    freezer.move_to(NOW)
    site = _Nightscout(aioclient_mock, name="RawMetadata", v3=v3)
    raw_time = "2026-01-08T12:07:00+00:00"
    raw_stamp = site.put("raw-current", raw_time, raw_glucose, direction=raw_direction)
    site.bgnow = {"last": 162, "mills": _ms(raw_time)}
    site.calculations = {"delta": {"mgdl": 7}}
    if v2_direction is not None:
        site.calculations["direction"] = {"value": v2_direction}

    entry = await _configure(hass, site)
    try:
        route = ENTRIES if v3 else LEGACY_ENTRIES
        assert (route, [(_ms(raw_time), raw_glucose, raw_stamp)]) in site.raw_batches
        _current(hass, entry, str(raw_glucose), raw_time)
        reading = hass.states.get(_entity(hass, entry))
        assert reading.attributes.get("delta") == expected_delta
        assert reading.attributes.get("direction") == raw_direction
        delta = hass.states.get(_entity(hass, entry, "glucose_delta"))
        assert delta.state == (STATE_UNKNOWN if expected_delta is None else str(expected_delta))
        assert hass.states.get(_entity(hass, entry, "glucose_trend")).state == expected_trend
    finally:
        await _unload(hass, entry)


@pytest.mark.parametrize("v3", [False, True], ids=["legacy", "v3"])
@pytest.mark.parametrize("direction, trend", [("TripleUp", "triple_up"), ("TripleDown", "triple_down")])
async def test_raw_stock_triple_trend(hass, aioclient_mock, freezer, v3, direction, trend):
    freezer.move_to(NOW)
    site = _Nightscout(aioclient_mock, name="RawTrend", v3=v3)
    raw_time = "2026-01-08T12:07:00+00:00"
    site.put("raw-current", raw_time, 162, direction=direction)

    entry = await _configure(hass, site)
    try:
        _current(hass, entry, "162", raw_time)
        assert hass.states.get(_entity(hass, entry)).attributes["direction"] == direction
        state = hass.states.get(_entity(hass, entry, "glucose_trend"))
        assert state.state == trend
        assert trend in state.attributes["options"]
    finally:
        await _unload(hass, entry)


async def test_legacy_v2_fallback_keeps_its_own_metadata(hass, aioclient_mock, freezer):
    freezer.move_to(NOW)
    site = _Nightscout(aioclient_mock, name="LegacyMetadata", v3=False)
    fallback_time = "2026-01-08T12:07:00+00:00"
    site.bgnow = {"last": 162, "mills": _ms(fallback_time)}
    site.calculations = {"delta": {"mgdl": 7}, "direction": {"value": "Flat"}}

    entry = await _configure(hass, site)
    try:
        assert (LEGACY_ENTRIES, []) in site.raw_batches
        _current(hass, entry, "162", fallback_time)
        reading = hass.states.get(_entity(hass, entry))
        assert reading.attributes["delta"] == 7
        assert reading.attributes["direction"] == "Flat"
        assert hass.states.get(_entity(hass, entry, "glucose_delta")).state == "7"
        assert hass.states.get(_entity(hass, entry, "glucose_trend")).state == "flat"
    finally:
        await _unload(hass, entry)
