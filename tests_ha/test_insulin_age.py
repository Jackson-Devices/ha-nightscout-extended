"""Insulin age through Home Assistant setup and ordinary refresh."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from homeassistant.const import CONF_API_KEY, CONF_URL, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nightscout_extended.api import NightscoutClient
from custom_components.nightscout_extended.const import DOMAIN

URL = "https://fixture.invalid"
NOW = datetime(2026, 1, 8, 12, tzinfo=timezone.utc)
PROPS = json.loads(
    (Path(__file__).resolve().parents[1] / "tests/fixtures/properties.json").read_text()
)


async def test_insulin_age_manifest_and_warm_availability(
    hass: HomeAssistant, aioclient_mock, freezer, monkeypatch,
):
    freezer.move_to(NOW)
    aioclient_mock.get(f"{URL}/api/v1/status.json", json={"settings": {"units": "mg/dl"}})
    aioclient_mock.get(f"{URL}/api/v2/properties/bgnow", json=PROPS)
    aioclient_mock.get(
        f"{URL}/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal", json=PROPS,
    )
    original = NightscoutClient._get
    history = [{"_id": "first", "eventType": "Insulin Change", "created_at": "2026-01-05T00:00:00Z"}]
    denied = False

    async def legacy_get(self, path, query=None, **kwargs):
        if path == "/api/v1/treatments.json":
            if denied:
                from custom_components.nightscout_extended.api import NightscoutAuthError

                raise NightscoutAuthError("Denied treatment read", status=403)
            if "find[eventType][$in][0]" in query:
                return history.copy()
            return [{"_id": "bolus", "eventType": "Correction Bolus", "insulin": 2.5,
                     "created_at": "2026-01-08T11:50:00Z"}]
        return await original(self, path, query, **kwargs)

    monkeypatch.setattr(NightscoutClient, "_get", legacy_get)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, title="NS", data={CONF_URL: URL, CONF_API_KEY: ""})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_id = "sensor.ns_insulin_age"
    registry = er.async_get(hass)
    registered = registry.async_get(entity_id)
    assert registered.unique_id == f"{URL}_insulin_age"
    first = hass.states.get(entity_id)
    assert float(first.state) == 84
    assert first.attributes["unit_of_measurement"] == "h"
    assert first.attributes["source_time"] == "2026-01-05T00:00:00+00:00"
    assert first.attributes["event_type"] == "Insulin Change"

    freezer.move_to(NOW + timedelta(hours=1))
    await entry.runtime_data.async_refresh()
    continuing = hass.states.get(entity_id)
    assert float(continuing.state) == 85
    assert continuing.attributes["source_time"] == first.attributes["source_time"]

    freezer.move_to(NOW + timedelta(hours=2))
    history.clear()
    await entry.runtime_data.async_refresh()
    missing = hass.states.get(entity_id)
    assert missing.state == STATE_UNKNOWN
    assert "source_time" not in missing.attributes
    assert "event_type" not in missing.attributes

    denied = True
    freezer.move_to(NOW + timedelta(hours=3))
    await entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.ns_glucose").state == "155"

    denied = False
    history.append({"_id": "new", "eventType": "Insulin Change", "created_at": "2026-01-08T10:00:00Z"})
    freezer.move_to(NOW + timedelta(hours=4))
    await entry.runtime_data.async_refresh()
    recovered = hass.states.get(entity_id)
    assert float(recovered.state) == 6
    assert recovered.attributes["source_time"] == "2026-01-08T10:00:00+00:00"
    assert registry.async_get(entity_id).id == registered.id
    assert await hass.config_entries.async_unload(entry.entry_id)
