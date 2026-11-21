"""Exercise the cannula age entity with equivalent v1 and v3 treatments."""

from time import time

import pytest
from homeassistant.const import STATE_UNKNOWN
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from test_v3_integration import (
    BEARER,
    EXCHANGE,
    PROPERTIES,
    TREATMENTS_V1,
    TREATMENTS_V3,
    URL,
    VERSION,
    _configure,
    _entity_ids,
    _mock_reservoir,
    _mock_v1_sgv,
    _mock_v3_sgv,
    _ms,
    _refresh,
    _state,
)

NOW = "2026-01-08T17:00:00+00:00"
CHANGE = "2026-01-06T07:00:00+02:00"
CHANGE_UTC = "2026-01-06T05:00:00+00:00"
NEW_INSULIN = "2026-01-08T15:00:00+00:00"


def _mock_site(mocker, rows, *, v3):
    mocker.get(f"{URL}/api/v1/status.json", json={"settings": {"customTitle": "Parity Site"}})
    properties = {"bgnow": {"last": 153, "mills": _ms(NOW)}}
    mocker.get(f"{URL}/api/v2/properties/bgnow", json=properties)
    mocker.get(f"{URL}{PROPERTIES}", json=properties)
    if v3:
        now = int(time())
        mocker.get(f"{URL}{VERSION}", json={"status": 200, "result": {"apiVersion": "3.0.5"}})
        mocker.get(f"{URL}{EXCHANGE}", json={"token": BEARER, "iat": now, "exp": now + 86400})
        _mock_v3_sgv(mocker, ({
            "identifier": "cannula-fixture-glucose", "type": "sgv",
            "sgv": 153, "date": _ms(NOW), "srvModified": _ms(NOW),
        },))
    else:
        mocker.get(f"{URL}{VERSION}", status=404)
        _mock_v1_sgv(mocker)

    _mock_reservoir(mocker, v3)

    async def treatments(method, url, _data):
        if v3:
            kinds = url.query.get("eventType$in", "").split("|")
            records = [
                {"identifier": uid, "date": _ms(when), "eventType": kind}
                for uid, kind, when in rows if kind in kinds
            ]
            body = {"status": 200, "result": records}
        else:
            kinds = [value for key, value in url.query.items() if key.startswith("find[eventType][$in][")]
            body = [
                {"_id": uid, "created_at": when, "eventType": kind}
                for uid, kind, when in rows if kind in kinds
            ]
        return AiohttpClientMockResponse(method, url, status=200, json=body)

    mocker.get(f"{URL}{TREATMENTS_V3 if v3 else TREATMENTS_V1}", side_effect=treatments)


@pytest.mark.parametrize("v3", [False, True], ids=["v1", "v3"])
async def test_cannula_age_uses_its_own_latest_site_change(hass, aioclient_mock, freezer, v3):
    freezer.move_to(NOW)
    future = ("site-future", "Site Change", "2026-01-09T00:00:00+00:00")
    rows = [
        ("site-old", "Site Change", "2026-01-05T00:00:00+00:00"),
        future,
        ("insulin", "Insulin Change", CHANGE),
        ("site", "Site Change", CHANGE),
    ]
    _mock_site(aioclient_mock, rows, v3=v3)
    entry = await _configure(hass)

    try:
        cannula = _state(hass, "cannula_age")
        insulin = _state(hass, "insulin_age")
        assert _entity_ids(hass, entry)[f"{URL}_cannula_age"] == cannula.entity_id
        assert float(cannula.state) == float(insulin.state) == 60
        assert cannula.attributes["unit_of_measurement"] == "h"
        assert cannula.attributes["source_time"] == insulin.attributes["source_time"] == CHANGE_UTC
        assert cannula.attributes["event_type"] == "Site Change"
        assert insulin.attributes["event_type"] == "Insulin Change"

        new_insulin = ("insulin-new", "Insulin Change", NEW_INSULIN)
        rows.append(new_insulin)
        await _refresh(hass, entry)
        assert float(_state(hass, "insulin_age").state) == 2
        assert _state(hass, "insulin_age").attributes["source_time"] == NEW_INSULIN
        assert float(_state(hass, "cannula_age").state) == 60
        assert _state(hass, "cannula_age").attributes["source_time"] == CHANGE_UTC

        rows[:] = [future, new_insulin]
        await _refresh(hass, entry)
        assert _state(hass, "cannula_age").state == STATE_UNKNOWN
        assert float(_state(hass, "insulin_age").state) == 2
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
