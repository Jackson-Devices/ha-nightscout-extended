"""Load the actual HA entry and bundled card for one selected native replay case."""

import json
import os
from pathlib import Path
import sys
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from math import floor

import pytest

from tests_contract.math_review.native_replay import (
    CASES, ENABLE, MODES, epoch_ms, instant, is_loopback, write_observation,
)

pytest_plugins = ("pytest_homeassistant_custom_component",)

SIGNALS = (
    "glucose", "glucose_delta", "glucose_trend", "last_reading", "forecast_15", "forecast_30",
)
SENSOR_FIELDS = ("state", "unit", "last_changed", "last_updated")
SENSOR_ATTRIBUTES = (
    "source_time", "forecast_time", "forecast", "model", "direction", "delta",
    "thresholds_mgdl", "sgv_history_revision",
)


@pytest.fixture
def native_input():
    if os.environ.get(ENABLE) != "1":
        pytest.fail("Run native_replay.py to provide the disposable loopback server", pytrace=False)
    payload = json.load(sys.stdin)
    mode = payload["mode"]
    subject = payload["subject"]
    if (
        mode not in MODES or not is_loopback(payload["url"]) or payload["fixture"]["case"] not in CASES
        or (mode == "protected" and not isinstance(subject, str))
        or (mode == "public" and subject is not None)
    ):
        pytest.fail("Invalid disposable native replay input", pytrace=False)
    return payload


def _fields(source, fields):
    return {key: source.get(key) for key in fields}


def _state_observation(combined):
    """Retain the observed fields and discard registry IDs and arbitrary attrs."""
    ha = combined["ha"]
    states = {}
    for key in SIGNALS:
        state = ha["states"].get(ha["entity_ids"].get(key), {})
        states[key] = {
            **_fields(state, SENSOR_FIELDS),
            "attributes": _fields(state.get("attributes", {}), SENSOR_ATTRIBUTES),
        }

    history = ha["history"]
    card = combined["card"]
    return {
        "states": states,
        "history": {
            **_fields(history, ("unit", "status", "as_of")),
            "readings": [_fields(row, ("id", "date", "sgv", "direction")) for row in history["readings"]],
            "missing_ranges": [_fields(row, ("start", "end")) for row in history["missing_ranges"]],
        },
        "card": {
            **_fields(card, ("value", "delta", "arrow", "age", "footer", "now_ms", "card_version")),
            "classification": _fields(
                card["classification"], ("mgdl", "age_min", "stale_minutes", "level", "thresholds"),
            ),
            "chart": _fields(
                card["chart"], ("history_mode", "status", "points", "measured_path", "forecast_path"),
            ),
        },
    }


def _quantity_matches(state, quantity):
    try:
        expected = quantity if isinstance(quantity, Fraction) else Fraction(Decimal(str(quantity)))
        return Fraction(Decimal(state)) == expected
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return False


def _ms_matches(iso, expected):
    try:
        return isinstance(iso, str) and epoch_ms(iso) == expected
    except (TypeError, ValueError, OverflowError):
        return False


def _forecast_checks(mgdl, native, targets):
    states = mgdl["states"]
    points = sorted(native["forecast"], key=lambda point: point["mills"])
    checks = {}
    # Use the independent temporal selection on actual emitted wire points.
    # Availability is evaluated separately for each requested horizon.
    for horizon, selected in targets.items():
        key = f"forecast_{horizon}"
        state = states[key]
        attributes = state["attributes"]
        if selected is None:
            checks[f"{key}_absent"] = (
                state["state"] in {"unknown", "unavailable"} and attributes["forecast_time"] is None
            )
            continue
        millis, quantity = selected
        checks[f"{key}_wire_value"] = _quantity_matches(state["state"], quantity)
        checks[f"{key}_wire_source_time"] = _ms_matches(attributes["source_time"], native["bgnow"]["mills"])
        checks[f"{key}_wire_forecast_time"] = _ms_matches(attributes["forecast_time"], millis)
    curve = states["forecast_30"]["attributes"]["forecast"] or []
    checks["forecast_curve_wire_points"] = len(curve) == len(points) and all(
        _ms_matches(rendered.get("time"), point["mills"]) and rendered.get("mgdl") == point["mgdl"]
        for rendered, point in zip(curve, points, strict=True)
    )
    selected_30 = targets[30]
    footer = [part for part in mgdl["card"]["footer"] if part.startswith("30 min ≈ ")]
    checks["forecast_footer_wire_30"] = footer == (
        [] if selected_30 is None else [f"30 min ≈ {floor(selected_30[1] + Fraction(1, 2))}"]
    )
    return checks


def _checks(mgdl, mmol, native, fixture, targets, *, actual_v3, protected):
    latest = fixture["readings"][-1]
    expected_display_timestamp = latest["date"] // 1000 * 1000
    expected_history = fixture["readings"]
    points = [[row["date"], row["sgv"]] for row in expected_history]
    thresholds = native["thresholds"]
    alarm_band = {
        "urgent_low": thresholds["bgLow"], "low": thresholds["bgTargetBottom"],
        "high": thresholds["bgTargetTop"], "urgent_high": thresholds["bgHigh"],
    }
    native_delta = native["delta"]["mgdl"]
    delta_state = mgdl["states"]["glucose_delta"]["state"]
    return {
        "configured_transport": actual_v3 is protected,
        "source_exact_threshold": latest["sgv"] in {alarm_band["low"], alarm_band["high"]},
        "ha_glucose_matches_native_bgnow": _quantity_matches(
            mgdl["states"]["glucose"]["state"], native["bgnow"]["last"],
        ),
        "ha_last_reading_public_seconds": _ms_matches(
            mgdl["states"]["last_reading"]["state"], expected_display_timestamp,
        ),
        "ha_delta_matches_v2_properties": (
            delta_state in {"unknown", "unavailable"} if native_delta is None
            else _quantity_matches(delta_state, native_delta)
        ),
        "ha_history_original_ids_and_ms": all(
            observed["history"]["readings"] == expected_history and
            observed["history"]["unit"] == "mg/dL" and observed["history"]["status"] == "complete" and
            observed["history"]["missing_ranges"] == []
            for observed in (mgdl, mmol)
        ),
        "ha_history_as_of_is_poll_clock": all(
            _ms_matches(observed["history"]["as_of"], fixture["poll_ms"]) for observed in (mgdl, mmol)
        ),
        "ha_registry_unit_override": (
            mgdl["states"]["glucose"]["unit"] == "mg/dL" and
            mmol["states"]["glucose"]["unit"] == "mmol/L"
        ),
        "card_exact_mgdl_display": mgdl["card"]["value"] == str(latest["sgv"]),
        "card_mmol_display_quantization": mmol["card"]["value"] == fixture["mmol_display"],
        "card_thresholds_from_native_site": all(
            observed["card"]["classification"]["thresholds"] == alarm_band for observed in (mgdl, mmol)
        ),
        "card_mgdl_equal_is_in_range": mgdl["card"]["classification"]["level"] == "in_range",
        # This is a review law, independent of rendering a rounded unit
        # label. A boundary classification flip remains a semantic finding.
        "card_mmol_equal_is_in_range": mmol["card"]["classification"]["level"] == "in_range",
        "card_history_preserves_actual_points": all(
            observed["card"]["chart"]["history_mode"] == "source" and
            observed["card"]["chart"]["status"] == "complete" and
            observed["card"]["chart"]["points"] == points
            for observed in (mgdl, mmol)
        ),
        "card_browser_clock_is_independent": all(
            observed["card"]["now_ms"] == fixture["browser_ms"] and
            observed["card"]["classification"]["age_min"] == (
                fixture["browser_ms"] - expected_display_timestamp
            ) / 60_000
            for observed in (mgdl, mmol)
        ),
        **_forecast_checks(mgdl, native, targets),
    }


@pytest.mark.asyncio
async def test_native_math(hass, freezer, hass_storage, enable_custom_integrations, native_input):
    # hass_storage patches stock HA's config store to memory for this test.
    # The real config flow receives the disposable subject and selects its
    # transport. The API client, history, state machine and JS card are real.
    from homeassistant import config_entries
    from homeassistant.const import CONF_API_KEY, CONF_URL
    from homeassistant.data_entry_flow import FlowResultType
    from homeassistant.helpers import entity_registry as er

    from custom_components.nightscout_extended.const import DOMAIN
    from tests_contract.math_review.oracle import expected_forecast
    from tests_contract.math_review.projection import capture_states, observe_projection

    launch = native_input
    protected = launch["mode"] == "protected"
    fixture, native = launch["fixture"], launch["native"]
    output = Path(launch["output"])
    freezer.move_to(instant(fixture["poll_ms"]))
    record = {"case": fixture["case"], "mode": launch["mode"], "fixture": fixture, "native": native}
    emitted = [(point["mills"], str(point["mgdl"])) for point in native["forecast"]]
    try:
        targets = {
            horizon: expected_forecast(native["bgnow"]["mills"], horizon, emitted)
            for horizon in (15, 30)
        }
    except (TypeError, ValueError, InvalidOperation):
        write_observation(output, {**record, "infrastructure_phase": "forecast_input_ambiguity"})
        pytest.fail("Native forecast observations have no unambiguous temporal target", pytrace=False)
    record["reference_forecast_targets"] = {
        str(horizon): None if point is None else {"mills": point[0], "mgdl_exact": str(point[1])}
        for horizon, point in targets.items()
    }

    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert form["type"] is FlowResultType.FORM
    form_input = {CONF_URL: launch["url"]}
    if protected:
        form_input[CONF_API_KEY] = launch["subject"]
    outcome = await hass.config_entries.flow.async_configure(form["flow_id"], form_input)
    if outcome["type"] is not FlowResultType.CREATE_ENTRY:
        write_observation(output, {**record, "checks": {"ha_config_created": False}})
        pytest.fail("The actual integration rejected the synthetic site", pytrace=False)

    entry = outcome["result"]
    await hass.async_block_till_done()
    if entry.state is not config_entries.ConfigEntryState.LOADED:
        write_observation(output, {**record, "checks": {"ha_entry_loaded": False}})
        pytest.fail("The actual integration did not load the synthetic site", pytrace=False)

    options = {"now_ms": fixture["browser_ms"], "node": launch["node"], "config": {"hours_to_show": 1}}
    try:
        ids = capture_states(hass, entry)["entity_ids"]
        registry = er.async_get(hass)
        observations = {}
        for label, unit, precision in (("mgdl", "mg/dL", 0), ("mmol", "mmol/L", 1)):
            for key in ("glucose", "glucose_delta", "forecast_15", "forecast_30"):
                registry.async_update_entity_options(
                    ids[key], "sensor", {"unit_of_measurement": unit, "display_precision": precision},
                )
            await hass.async_block_till_done()
            combined = await observe_projection(hass, entry, **options)
            observations[label] = _state_observation(combined)
        record["observations"] = observations
        record["checks"] = _checks(
            observations["mgdl"], observations["mmol"], native, fixture, targets,
            actual_v3=entry.runtime_data.uses_v3, protected=protected,
        )
        write_observation(output, record)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)

    assert all(record["checks"].values()), "See allowlisted native math observation"
