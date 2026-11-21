"""Exercise CGM sensor age in Home Assistant with synthetic v1 and v3 records."""

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, CONF_API_KEY, CONF_URL, UnitOfTime
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nightscout_extended.api import NightscoutClient, NightscoutConnectionError
from custom_components.nightscout_extended.const import DOMAIN

NOW = datetime(2026, 1, 16, 12, tzinfo=timezone.utc)
SENSOR_CHANGED = NOW - timedelta(hours=108)
OLDER_START = SENSOR_CHANGED - timedelta(days=4)


@pytest.mark.parametrize("mode", ["legacy", "v3"])
async def test_cgm_sensor_age_keeps_event_time_across_reading_and_backfill(
    hass, freezer, monkeypatch, mode,
):
    freezer.move_to(NOW)
    markers = [("sensor-change", "Sensor Change", SENSOR_CHANGED)]
    reading = (NOW, 120)
    age_reads = []

    def treatment_rows(event_types):
        return [
            {
                "eventType": event_type,
                **(
                    {"identifier": uid, "date": int(when.timestamp() * 1000)}
                    if mode == "v3"
                    else {"_id": uid, "created_at": when.isoformat()}
                ),
            }
            for uid, event_type, when in markers
            if event_type in event_types
        ]

    async def request(self, path, query=None, **kwargs):
        query = query or {}
        if path == "/api/v1/status.json":
            return {"settings": {"units": "mg/dl"}}
        if path.startswith("/api/v2/properties/"):
            when, glucose = reading
            return {"bgnow": {"last": glucose, "mills": int(when.timestamp() * 1000)}}
        if path == "/api/v3/version":
            if mode == "legacy":
                raise NightscoutConnectionError("Synthetic legacy route", status=404)
            return {"status": 200, "result": {"apiVersion": "3.0.5"}}
        if path.startswith("/api/v2/authorization/request/"):
            assert mode == "v3"
            return {
                "token": "synthetic-cgm-bearer",
                "iat": NOW.timestamp(),
                "exp": (NOW + timedelta(days=1)).timestamp(),
            }
        if path in {"/api/v1/entries.json", "/api/v1/devicestatus/"}:
            return []
        if path == "/api/v3/devicestatus":
            return {"status": 200, "result": []}
        if path == "/api/v3/lastModified":
            return {"status": 200, "result": {"srvDate": int(NOW.timestamp() * 1000)}}
        if path == "/api/v3/entries" or path.startswith("/api/v3/entries/history/"):
            when, glucose = reading
            stamp = int(when.timestamp() * 1000)
            if path == "/api/v3/entries":
                present = int(query["date$gte"]) <= stamp < int(query["date$lt"])
            else:
                present = stamp > int(path.rsplit("/", 1)[1])
            row = {
                "identifier": f"fixture-{stamp}", "type": "sgv",
                "date": stamp, "srvModified": stamp, "sgv": glucose,
            }
            return {"status": 200, "result": [row] if present else []}
        if path == "/api/v1/treatments.json":
            assert mode == "legacy"
            event_types = {
                value for key, value in query.items() if key.startswith("find[eventType][$in][")
            }
            if event_types:
                age_reads.append(event_types)
            return treatment_rows(event_types)
        if path == "/api/v3/treatments":
            assert mode == "v3"
            event_types = set(query.get("eventType$in", "").split("|")) - {""}
            if event_types:
                age_reads.append(event_types)
            return {"status": 200, "result": treatment_rows(event_types)}
        pytest.fail("The CGM fixture received an unexpected request")

    monkeypatch.setattr(NightscoutClient, "_request", request)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="https://ns.example.com",
        title="NS",
        data={CONF_URL: "https://ns.example.com", CONF_API_KEY: "synthetic-cgm-subject"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    def assert_age(hours):
        age = hass.states.get("sensor.ns_cgm_sensor_age")
        assert age is not None
        assert float(age.state) == hours
        assert age.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTime.HOURS
        assert age.attributes["source_time"] == SENSOR_CHANGED.isoformat()
        assert age.attributes["event_type"] == "Sensor Change"

    assert_age(108)
    assert any({"Sensor Start", "Sensor Change"} <= kinds for kinds in age_reads)

    # A new SGV and later-arriving historical treatment leave the age origin intact.
    freezer.move_to(NOW + timedelta(hours=1))
    reading = (NOW + timedelta(hours=1), 131)
    markers.insert(0, ("backfilled-sensor-start", "Sensor Start", OLDER_START))
    age_reads.clear()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert any({"Sensor Start", "Sensor Change"} <= kinds for kinds in age_reads)
    assert float(hass.states.get("sensor.ns_glucose").state) == 131
    assert_age(109)
