"""Synthetic HA-to-card math observations and separately named candidate hypotheses."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from tests_ha.test_sgv_ingestion import PROPERTIES, _Nightscout, _configure, _unload
from tests_contract.math_review.oracle import classify_mgdl
from tests_contract.math_review.projection import capture_states, observe_projection

pytest_plugins = ["pytest_homeassistant_custom_component"]

SOURCE_MS = 1_767_873_600_000  # 2026-01-08T12:00:00Z
MINUTE_MS = 60_000
HA_NOW_MS = SOURCE_MS + MINUTE_MS
DEFAULT_THRESHOLDS = {"urgent_low": 52, "low": 77, "high": 180, "urgent_high": 265}
NIGHTSCOUT_THRESHOLDS = {"bgLow": 52, "bgTargetBottom": 77, "bgTargetTop": 180, "bgHigh": 265}
DAY_BG = {
    "stale": "#5f6368", "urgent_low": "#c62828", "low": "#e8615a",
    "in_range": "#3d9a5b", "high": "#f2df74", "urgent_high": "#ef7a78",
}
MGDL, MMOL = "mg/dL", "mmol/L"
# HA 2026.10.4 BloodGlucoseConcentrationConverter uses 180.16 g/mol.
# This is the state conversion oracle, separate from the card's own factor.
HA_MGDL_PER_MMOL = Decimal("18.016")


def _iso_ms(epoch_ms):
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=epoch_ms)).isoformat(timespec="milliseconds")


class _ProjectionSite(_Nightscout):
    """Preserve source clocks while supplying independently specified raw quantities."""

    def __init__(self, mocker, raw_glucose, delta, source_ms):
        super().__init__(mocker, name="Math")
        self.properties = {"iob": {"iob": 1.25}, "cob": {"cob": 12}}
        self.publish(
            "measurement", raw_glucose, delta, source_ms,
            ((source_ms + 15 * MINUTE_MS + 2000, 165.25), (source_ms + 30 * MINUTE_MS + 2000, 180.5)),
        )

    def set_curve(self, points):
        self.properties["ar2"] = {
            "forecast": {"predicted": [{"mills": date, "mgdl": value} for date, value in points]},
        }

    def publish(self, identity, raw_glucose, delta, source_ms, points):
        self.bgnow = {"last": float(raw_glucose), "mills": source_ms}
        self.properties.update({
            "bgnow": self.bgnow, "delta": {"mgdl": float(delta)}, "direction": {"value": "FortyFiveUp"},
        })
        self.set_curve(points)
        self.put(identity, _iso_ms(source_ms), float(raw_glucose), direction="FortyFiveUp")
        self.docs[identity]["date"] = source_ms

    async def _answer(self, method, url, data):
        if url.path == PROPERTIES:
            return AiohttpClientMockResponse(method, url, json=self.properties)
        if url.path == "/api/v1/status.json":
            return AiohttpClientMockResponse(
                method, url, json={
                    "version": "15.0.6",
                    "settings": {"units": "mg/dl", "customTitle": self.name, "thresholds": NIGHTSCOUT_THRESHOLDS},
                },
            )
        return await super()._answer(method, url, data)


@asynccontextmanager
async def _loaded(hass, mocker, freezer, raw_glucose, *, delta="0.5", source_ms=SOURCE_MS):
    freezer.move_to(_iso_ms(HA_NOW_MS))
    site = _ProjectionSite(mocker, raw_glucose, delta, source_ms)
    entry = await _configure(hass, site)
    try:
        yield entry, site
    finally:
        await _unload(hass, entry)


def _state(bundle, key):
    return bundle["states"][bundle["entity_ids"][key]]


async def _set_units(hass, entry, **units):
    bundle = capture_states(hass, entry)
    registry = er.async_get(hass)
    for key, unit in units.items():
        registry.async_update_entity_options(
            bundle["entity_ids"][key], "sensor", {
                "unit_of_measurement": unit, "display_precision": 1 if unit == MMOL else 0,
            },
        )
    await hass.async_block_till_done()


def _assert_sensor(bundle, key, source_mgdl, unit):
    state = _state(bundle, key)
    assert state["unit"] == unit
    expected = Decimal(source_mgdl)
    if unit == MMOL:
        expected /= HA_MGDL_PER_MMOL
    assert Decimal(state["state"]) == pytest.approx(expected, abs=Decimal("0.0000000001"))


def _assert_history(observed, source_mgdl, source_ms=SOURCE_MS):
    assert observed["ha"]["history"]["readings"] == [
        {"id": "measurement", "date": source_ms, "sgv": float(source_mgdl), "direction": "FortyFiveUp"}
    ]
    assert observed["card"]["chart"]["history_mode"] == "source"
    assert observed["card"]["chart"]["points"] == [[source_ms, float(source_mgdl)]]
    assert observed["card"]["chart"]["measured_path"]


async def _refresh_at(hass, entry, freezer, now_ms):
    freezer.move_to(_iso_ms(now_ms))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.last_update_success


def _assert_forecast_clock(bundle, key, source_ms, point, expected_state):
    state = _state(bundle, key)
    assert state["state"] == expected_state
    assert datetime.fromisoformat(state["attributes"]["source_time"]) == datetime.fromisoformat(_iso_ms(source_ms))
    target = state["attributes"]["forecast_time"]
    if point is None:
        assert target is None
    else:
        assert datetime.fromisoformat(target) == datetime.fromisoformat(_iso_ms(point[0]))


@pytest.mark.parametrize(
    "source_mgdl",
    [
        pytest.param("51.5", id="below-urgent-low"),
        pytest.param("52", id="at-urgent-low"),
        pytest.param("76.9", id="below-low"),
        pytest.param("77", id="at-low"),
        pytest.param("179.9", id="below-180"),
        pytest.param("180", id="at-180"),
        pytest.param("180.1", id="above-180"),
        pytest.param("265", id="at-urgent-high"),
        pytest.param("265.1", id="above-urgent-high"),
    ],
)
async def test_projection_classifies_mgdl_at_configured_boundaries(
    hass, aioclient_mock, freezer, enable_custom_integrations, source_mgdl,
):
    async with _loaded(hass, aioclient_mock, freezer, source_mgdl) as (entry, _):
        await _set_units(hass, entry, glucose=MGDL)
        observed = await observe_projection(hass, entry, now_ms=HA_NOW_MS)
        _assert_sensor(observed["ha"], "glucose", source_mgdl, MGDL)
        _assert_history(observed, source_mgdl)
        card = observed["card"]
        zone = classify_mgdl(source_mgdl, DEFAULT_THRESHOLDS)
        assert card["classification"]["thresholds"] == DEFAULT_THRESHOLDS
        assert card["classification"]["level"] == zone
        assert card["background"] == DAY_BG[zone]
        assert card["value"] == str(Decimal(source_mgdl).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        assert card["arrow"] == "↗"


@pytest.mark.parametrize("source_mgdl", ["180", "181"], ids=["at-180", "above-180"])
async def test_hypothesis_projection_alarm_band_is_unit_invariant(
    hass, aioclient_mock, freezer, enable_custom_integrations, source_mgdl,
):
    """Candidate risk: HA uses 18.016, while the card converts units with 18.0182."""
    async with _loaded(hass, aioclient_mock, freezer, source_mgdl) as (entry, _):
        await _set_units(hass, entry, glucose=MGDL)
        mg = await observe_projection(hass, entry, now_ms=HA_NOW_MS)
        await _set_units(hass, entry, glucose=MMOL)
        mmol = await observe_projection(hass, entry, now_ms=HA_NOW_MS)
        _assert_sensor(mg["ha"], "glucose", source_mgdl, MGDL)
        _assert_sensor(mmol["ha"], "glucose", source_mgdl, MMOL)
        _assert_history(mg, source_mgdl)
        _assert_history(mmol, source_mgdl)
        assert mg["card"]["value"] == source_mgdl
        # Distinct measurements can quantize to the same formatted number.
        assert mmol["card"]["value"] == "10.0"
        zone = classify_mgdl(source_mgdl, DEFAULT_THRESHOLDS)
        assert mg["card"]["classification"]["level"] == zone
        assert (mmol["card"]["classification"]["level"], mmol["card"]["background"]) == (zone, DAY_BG[zone]), {
            "ha_glucose": _state(mmol["ha"], "glucose"),
            "classifier": mmol["card"]["classification"],
        }


@pytest.mark.parametrize(
    ("delta", "text"),
    [("0.5", "+1"), ("-0.5", "±0"), ("1.5", "+2"), ("-1.5", "−1")],
)
async def test_projection_formats_signed_mgdl_delta_at_halves(
    hass, aioclient_mock, freezer, enable_custom_integrations, delta, text,
):
    """Characterize the existing UI's Math.round tie direction."""
    async with _loaded(hass, aioclient_mock, freezer, "180.5", delta=delta) as (entry, _):
        await _set_units(hass, entry, glucose=MGDL, glucose_delta=MGDL, forecast_30=MGDL)
        observed = await observe_projection(hass, entry, now_ms=HA_NOW_MS)
        _assert_sensor(observed["ha"], "glucose", "180.5", MGDL)
        _assert_sensor(observed["ha"], "glucose_delta", delta, MGDL)
        _assert_sensor(observed["ha"], "forecast_30", "180.5", MGDL)
        assert observed["card"]["value"] == "181"
        assert observed["card"]["delta"] == text
        assert "30 min ≈ 181" in observed["card"]["footer"]


@pytest.mark.parametrize(
    ("primary", "siblings", "delta_text", "forecast_text"),
    [(MGDL, MMOL, "+10", "30 min ≈ 181"), (MMOL, MGDL, "+0.5", "30 min ≈ 10.0")],
    ids=["mgdl-primary", "mmol-primary"],
)
async def test_hypothesis_projection_formats_mixed_sibling_units_in_primary_unit(
    hass, aioclient_mock, freezer, enable_custom_integrations, primary, siblings, delta_text, forecast_text,
):
    """Ask whether the card converts each sibling's selected unit before formatting."""
    async with _loaded(hass, aioclient_mock, freezer, "180", delta="9.5") as (entry, _):
        await _set_units(hass, entry, glucose=primary, glucose_delta=siblings, forecast_15=primary, forecast_30=siblings)
        observed = await observe_projection(hass, entry, now_ms=HA_NOW_MS)
        for key, source, unit in (
            ("glucose", "180", primary), ("glucose_delta", "9.5", siblings),
            ("forecast_15", "165.25", primary), ("forecast_30", "180.5", siblings),
        ):
            _assert_sensor(observed["ha"], key, source, unit)
        card = observed["card"]
        forecasts = [segment for segment in card["footer"] if segment.startswith("30 min")]
        assert (card["delta"], forecasts) == (delta_text, [forecast_text])


async def test_projection_uses_browser_freshness_without_aging_ha_state(
    hass, aioclient_mock, freezer, enable_custom_integrations,
):
    async with _loaded(hass, aioclient_mock, freezer, "180") as (entry, site):
        await _set_units(hass, entry, glucose=MGDL, glucose_delta=MGDL, forecast_15=MGDL, forecast_30=MGDL)
        exact_ms = SOURCE_MS + 15 * MINUTE_MS
        fresh = await observe_projection(hass, entry, now_ms=exact_ms, config={"stale_minutes": 15})
        stale = await observe_projection(hass, entry, now_ms=exact_ms + 1, config={"stale_minutes": 15})
        assert fresh["ha"]["states"] == stale["ha"]["states"]
        assert fresh["card"]["classification"]["level"] == "in_range"
        assert fresh["card"]["classification"]["age_min"] == 15
        assert fresh["card"]["arrow"] == "↗"
        assert fresh["card"]["chart"]["forecast_path"]
        assert stale["card"]["classification"]["level"] == "stale"
        assert stale["card"]["background"] == DAY_BG["stale"]
        assert stale["card"]["arrow"] == stale["card"]["delta"] == ""
        assert stale["card"]["chart"]["forecast_path"] == ""
        assert not any(segment.startswith("30 min") for segment in stale["card"]["footer"])
        assert "30 min ≈ 181" in fresh["card"]["footer"]
        assert _state(stale["ha"], "forecast_30")["state"] == _state(fresh["ha"], "forecast_30")["state"]

        # HA now advances too. A successful poll of the same upstream reading
        # and curve keeps their source times and entity availability.
        quiet_ms = SOURCE_MS + 16 * MINUTE_MS
        await _refresh_at(hass, entry, freezer, quiet_ms)
        quiet = await observe_projection(hass, entry, now_ms=quiet_ms)
        _assert_history(quiet, "180")
        for key in ("glucose", "last_reading", "forecast_15", "forecast_30"):
            assert _state(quiet["ha"], key)["state"] == _state(fresh["ha"], key)["state"]
        for key in ("forecast_15", "forecast_30"):
            assert _state(quiet["ha"], key)["attributes"] == _state(fresh["ha"], key)["attributes"]
        assert quiet["card"]["classification"]["level"] == "stale"
        assert quiet["card"]["chart"]["forecast_path"] == ""

        # A newly uploaded measurement has its own ID, measurement time and
        # write time. Nightscout supplies a separate AR2 curve anchored to it.
        new_ms = quiet_ms + MINUTE_MS + 7
        points = ((new_ms + 15 * MINUTE_MS + 2000, 160.75), (new_ms + 30 * MINUTE_MS + 2000, 169.5))
        freezer.move_to(_iso_ms(new_ms + 1000))
        site.publish("new-measurement", "160.25", "-1.5", new_ms, points)
        next_poll_ms = quiet_ms + 2 * MINUTE_MS
        await _refresh_at(hass, entry, freezer, next_poll_ms)
        recovered = await observe_projection(hass, entry, now_ms=next_poll_ms)
        actual = recovered["ha"]
        assert actual["entity_ids"] == quiet["ha"]["entity_ids"]
        _assert_sensor(actual, "glucose", "160.25", MGDL)
        assert _state(actual, "last_reading")["state"] == "2026-01-08T12:17:00+00:00"
        assert actual["history"]["readings"] == [
            *quiet["ha"]["history"]["readings"],
            {"id": "new-measurement", "date": new_ms, "sgv": 160.25, "direction": "FortyFiveUp"},
        ]
        assert recovered["card"]["chart"]["points"] == [[SOURCE_MS, 180], [new_ms, 160.25]]
        _assert_forecast_clock(actual, "forecast_15", new_ms, points[0], "160.75")
        _assert_forecast_clock(actual, "forecast_30", new_ms, points[1], "169.5")
        assert recovered["card"]["classification"]["level"] == "in_range"
        assert recovered["card"]["value"] == "160"
        assert recovered["card"]["chart"]["forecast_path"]
        assert "30 min ≈ 170" in recovered["card"]["footer"]

        # The upstream curve now omits only +15. Keep the new measurement and
        # +30 forecast with their original source times.
        site.set_curve(points[1:])
        next_poll_ms += MINUTE_MS
        await _refresh_at(hass, entry, freezer, next_poll_ms)
        partial = await observe_projection(hass, entry, now_ms=next_poll_ms)
        assert partial["ha"]["history"]["readings"] == actual["history"]["readings"]
        _assert_forecast_clock(partial["ha"], "forecast_15", new_ms, None, STATE_UNKNOWN)
        _assert_forecast_clock(partial["ha"], "forecast_30", new_ms, points[1], "169.5")
        assert "30 min ≈ 170" in partial["card"]["footer"]


async def test_projection_keeps_measurement_milliseconds_in_history_and_card(
    hass, aioclient_mock, freezer, enable_custom_integrations,
):
    original_ms = SOURCE_MS + 7
    async with _loaded(hass, aioclient_mock, freezer, "180", source_ms=original_ms) as (entry, _):
        observed = await observe_projection(hass, entry, now_ms=HA_NOW_MS)
        _assert_history(observed, "180", original_ms)
        # HA timestamp state has whole seconds. Raw action and forecast clocks
        # carry the source milliseconds independently of this display layer.
        assert _state(observed["ha"], "last_reading")["state"] == "2026-01-08T12:00:00+00:00"
        for key, horizon in (("forecast_15", 15), ("forecast_30", 30)):
            attributes = _state(observed["ha"], key)["attributes"]
            assert datetime.fromisoformat(attributes["source_time"]) == datetime.fromisoformat(_iso_ms(original_ms))
            target = original_ms + horizon * MINUTE_MS + 2000
            assert datetime.fromisoformat(attributes["forecast_time"]) == datetime.fromisoformat(_iso_ms(target))
