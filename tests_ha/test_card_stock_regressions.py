"""Replay PR #1 display differences using the checkout's HA integration and card.

Run from the product repository. The review report gives the pytest command.
The reference values come from Nightscout 15.0.6 at 9cd304f.
"""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from homeassistant.const import CONF_URL
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nightscout_extended.api import PROPERTIES
from custom_components.nightscout_extended.const import DOMAIN


ROOT = Path.cwd()
CARD = ROOT / "custom_components/nightscout_extended/www/nightscout-glucose-card.js"
RENDERER = ROOT / "tests_parity/js/card_render.mjs"
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
URL = "https://ns.example.com"


def sgv(mgdl, minutes_ago=0):
    when = NOW - timedelta(minutes=minutes_ago)
    return {"mgdl": mgdl, "mills": int(when.timestamp() * 1000), "direction": "Flat"}


def properties(current, *, latest_raw=None, previous=None, change_display=None):
    rows = [current] if latest_raw is None else [latest_raw, current]
    props = {
        "bgnow": {"last": current["mgdl"], "mean": current["mgdl"], "mills": current["mills"], "sgvs": rows},
        "direction": {"value": rows[0]["direction"]},
    }
    if previous is not None:
        props["delta"] = {
            "absolute": current["mgdl"] - previous["mgdl"],
            "mgdl": current["mgdl"] - previous["mgdl"],
            "elapsedMins": (current["mills"] - previous["mills"]) / 60000,
            "interpolated": False,
            "mean5MinsAgo": previous["mgdl"],
            "times": {"recent": current["mills"], "previous": previous["mills"]},
            "previous": {"last": previous["mgdl"], "mean": previous["mgdl"],
                         "mills": previous["mills"], "sgvs": [previous]},
            "scaled": 0,
            "display": change_display,
        }
    return props


def render(states):
    payload = {"card": str(CARD), "now": NOW.isoformat(), "cases": [states]}
    result = subprocess.run(
        ["node", str(RENDERER)], input=json.dumps(payload), text=True,
        capture_output=True, check=True, timeout=20,
    )
    return json.loads(result.stdout)[0]


@pytest.mark.parametrize(
    ("props", "units", "warn_minutes", "bg_low", "surface", "expected"),
    [
        # updateCurrentSGV renders the newest raw 9 as an hourglass with empty numeric text.
        pytest.param(properties(sgv(250, 2), latest_raw=sgv(9, 1)), "mg/dl", 15, 55,
                     "text", "", id="newer-sensor-error"),
        # bgnow.calcDelta rounds each bucket mean to site units before subtracting.
        pytest.param(properties(sgv(101), previous=sgv(100, 5), change_display="+0"), "mmol", 15, 55,
                     "delta", "+0", id="mmol-delta-rounding"),
        pytest.param(properties(sgv(100), previous=sgv(100, 5), change_display="+0"), "mg/dl", 15, 55,
                     "delta", "+0", id="flat-mgdl-delta-text"),
        # timeago.checkStatus returns warn one minute beyond this site's five-minute limit.
        pytest.param(properties(sgv(120, 6)), "mg/dl", 5, 55,
                     "foreground", "#bdbdbd", id="site-freshness"),
        # With the served bgLow of 0, 45 is below bgTargetBottom but above bgLow.
        pytest.param(properties(sgv(45)), "mg/dl", 15, 0,
                     "foreground", "#ffff00", id="served-zero-threshold"),
        # scaleBg returns the current mg/dL reading with its original precision.
        pytest.param(properties(sgv(180.1)), "mg/dl", 15, 55,
                     "text", "180.1", id="fractional-mgdl-reading"),
    ],
)
async def test_card_matches_stock(
    props, units, warn_minutes, bg_low, surface, expected,
    hass, freezer, aioclient_mock, enable_custom_integrations,
):
    freezer.move_to(NOW)
    settings = {
        "units": units,
        "theme": "colors",
        "thresholds": {"bgLow": bg_low, "bgTargetBottom": 80, "bgTargetTop": 180, "bgHigh": 260},
        "alarmTimeagoWarn": True, "alarmTimeagoWarnMins": warn_minutes,
        "alarmTimeagoUrgent": True, "alarmTimeagoUrgentMins": 30,
        "alarmLow": False, "alarmUrgentLow": False, "alarmHigh": False, "alarmUrgentHigh": False,
    }
    aioclient_mock.get(f"{URL}/api/v1/status.json", json={"version": "15.0.6", "settings": settings})
    aioclient_mock.get(f"{URL}/api/v2/properties/bgnow", json={"bgnow": props["bgnow"]})
    aioclient_mock.get(f"{URL}/api/v2/properties/{PROPERTIES}", json=props)
    aioclient_mock.get(f"{URL}/api/v1/treatments.json", json=[])

    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, title="Parity", data={CONF_URL: URL})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    try:
        await hass.async_block_till_done()
        states = {
            "glucose": hass.states.get("sensor.parity_glucose").as_dict(),
            "delta": hass.states.get("sensor.parity_glucose_change").as_dict(),
            "last": hass.states.get("sensor.parity_last_reading").as_dict(),
        }
        observed = await hass.async_add_executor_job(render, states)
        if surface == "foreground":
            colour = observed["paint"]["foreground"].lower()
            actual = {"yellow": "#ffff00", "#ff0": "#ffff00"}.get(colour, colour)
        else:
            actual = observed[surface]
        assert actual == expected
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("served", "expected"),
    [
        (20, 20.0),
        ("20", 20.0),  # ALARM_TIMEAGO_WARN_MINS=20 is served as a string
        (" 25 ", 25.0),
        ("7.5", 7.5),
        (None, 15.0),
        ("", 15.0),
        (0, 15.0),
        (False, 15.0),
        ("0", 0.0),  # a non-empty string is truthy in JavaScript, and Number("0") is 0
        ("abc", None),  # Number("abc") is NaN: stock's `age > NaN` never fires
        ("1_0", None),
        ("Infinity", None),
    ],
)
def test_timeago_limit_follows_javascript(served, expected):
    """Mirror timeago.checkStatus: `alarmTimeagoWarnMins || 15`, then JavaScript arithmetic."""
    from custom_components.nightscout_extended.coordinator import timeago_limit_minutes

    assert timeago_limit_minutes(served, 15) == expected
