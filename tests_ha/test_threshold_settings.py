"""Exercise threshold settings through the normal Home Assistant setup path."""

import pytest
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.nightscout_extended.const import DEFAULT_THRESHOLDS_MGDL, DOMAIN

URL = "https://nightscout.example"


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        pytest.param(
            {"units": "mmol", "thresholds": {
                "bgLow": 61, "bgTargetBottom": 86, "bgTargetTop": 167, "bgHigh": 241,
            }},
            {"urgent_low": 61, "low": 86, "high": 167, "urgent_high": 241},
            id="native-thresholds-stay-in-mgdl",
        ),
        pytest.param(
            {"thresholds": {
                "bg_low": 62, "bg_target_bottom": 87, "bg_target_top": 168, "bg_high": 242,
            }},
            {"urgent_low": 62, "low": 87, "high": 168, "urgent_high": 242},
            id="documented-thresholds",
        ),
        pytest.param(
            {"thresholds": {
                "bgLow": "63.5", "bg_low": 64, "bg_target_bottom": "88.5",
                "bgTargetTop": 169, "bg_target_top": 170, "bgHigh": None, "bg_high": 243,
            }},
            {"urgent_low": 63.5, "low": 88.5, "high": 169, "urgent_high": 243},
            id="mixed-native-first-with-numeric-strings",
        ),
        pytest.param(
            {"thresholds": {
                "bgLow": True, "bg_low": 65,
                "bgTargetBottom": "malformed", "bg_target_bottom": 89,
                "bgTargetTop": "Infinity", "bg_target_top": 171,
                "bgHigh": "1e400", "bg_high": 244,
            }},
            {"urgent_low": 65, "low": 89, "high": 171, "urgent_high": 244},
            id="invalid-native-values-fall-back-to-documented",
        ),
        pytest.param(
            {"thresholds": {
                "bgLow": [], "bg_low": False,
                "bgTargetBottom": 0, "bg_target_bottom": -1,
                "bgTargetTop": "NaN", "bg_target_top": "1e400",
                "bg_high": 245,
            }},
            {"urgent_low": 55, "low": 80, "high": 180, "urgent_high": 245},
            id="invalid-pairs-use-only-their-own-defaults",
        ),
        pytest.param({}, DEFAULT_THRESHOLDS_MGDL, id="missing-thresholds"),
        pytest.param({"thresholds": ["bad"]}, DEFAULT_THRESHOLDS_MGDL, id="nonmapping-thresholds"),
        pytest.param(None, DEFAULT_THRESHOLDS_MGDL, id="null-settings"),
        pytest.param(["bad"], DEFAULT_THRESHOLDS_MGDL, id="nonmapping-settings"),
    ],
)
async def test_threshold_settings(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, settings, expected
):
    aioclient_mock.get(f"{URL}/api/v1/status.json", json={"settings": settings})
    aioclient_mock.get(f"{URL}/api/v2/properties/bgnow", json={})
    aioclient_mock.get(
        f"{URL}/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal", json={}
    )
    aioclient_mock.get(f"{URL}/api/v1/treatments.json", json=[])

    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, title="NS", data={CONF_URL: URL})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.thresholds_mgdl == expected

    assert await hass.config_entries.async_unload(entry.entry_id)
