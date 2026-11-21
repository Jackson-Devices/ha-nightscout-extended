"""Observe the released integration's AR2 entities through real HA setup and polling."""

from datetime import datetime, timedelta

from aiohttp import web
from homeassistant import config_entries
from homeassistant.const import CONF_URL, STATE_UNKNOWN
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_socket import socket_allow_hosts

from custom_components.nightscout_extended.const import DOMAIN


pytest_plugins = ["pytest_homeassistant_custom_component"]

STATUS = "/api/v1/status.json"
PROBE = "/api/v2/properties/bgnow"
PROPERTIES = "/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal"
TREATMENTS = "/api/v1/treatments.json"
DEVICESTATUS = "/api/v1/devicestatus/"
POLL_TIME = "2026-01-08T12:15:00Z"
AGE_EVENT_TYPES = ("Insulin Change", "Site Change", "Sensor Start", "Sensor Change", "Pump Battery Change")
AGE_FIELDS = tuple(f"find[eventType][$in][{index}]" for index in range(len(AGE_EVENT_TYPES)))
START = "find[created_at][$gte]"
END = "find[created_at][$lte]"
QUERY_KEYS = {"count", START, END, *AGE_FIELDS}


@pytest.fixture
async def forecast_site(socket_enabled):
    """Serve the released client's GET aliases on loopback only."""
    socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    data = {
        "mills": 1767873600000,
        "points": [],
        "requests": [],
    }
    uploaded_prediction = [
        {"device": "openaps://fixture", "openaps": {"suggested": {"predBGs": {"IOB": [145, 310, 320]}}}},
    ]

    async def read(request):
        query = {key: request.query[key] for key in request.query if key in QUERY_KEYS}
        anonymous_get = (
            request.method == "GET"
            and not request.can_read_body
            and len(query) == len(request.query)
            and not any(
                key in request.headers for key in ("Authorization", "api-secret", "Cookie", "Proxy-Authorization")
            )
        )
        # Unrecognized query parameters and credentials are recorded only by
        # the failed predicate, never by their values.
        data["requests"].append((request.path, query, anonymous_get))
        if request.path == STATUS:
            return web.json_response({"version": "15.0.9", "settings": {"units": "mg/dl"}})
        if request.path == TREATMENTS:
            return web.json_response([])
        if request.path == DEVICESTATUS:
            return web.json_response(uploaded_prediction)
        bgnow = {"last": 153, "mills": data["mills"]}
        if request.path == PROBE:
            return web.json_response({"bgnow": bgnow})
        return web.json_response({
            "bgnow": bgnow,
            "ar2": {"forecast": {"predicted": [{"mills": ms, "mgdl": bg} for ms, bg in data["points"]]}},
        })

    app = web.Application()
    for route in (STATUS, PROBE, PROPERTIES, TREATMENTS, DEVICESTATUS):
        app.router.add_get(route, read)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}", data
    finally:
        await runner.cleanup()


async def _setup(hass, url):
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER},
    )
    assert flow["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {CONF_URL: url})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED
    return entry


def _state(hass, entry, key):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.unique_id}_{key}")
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, key
    return state


def _forecasts(hass, entry):
    return _state(hass, entry, "forecast_15"), _state(hass, entry, "forecast_30")


async def _refresh(hass, entry, data):
    data["requests"].clear()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.last_update_success
    now = datetime.fromisoformat(POLL_TIME)

    def utc_ms(value):
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    treatment_poll = {
        START: utc_ms(now - timedelta(hours=48)), END: utc_ms(now + timedelta(days=62)), "count": "1000",
    }
    age_poll = {
        **dict(zip(AGE_FIELDS, AGE_EVENT_TYPES, strict=True)),
        START: "0001-01-01T00:00:00.000Z",
        END: utc_ms(now),
        "count": "100",
    }
    device_poll = {
        START: utc_ms(now - timedelta(hours=1)), END: utc_ms(now + timedelta(minutes=1)), "count": "1000",
    }

    # Calculated values, current treatments and age history are fetched
    # together. The device-status read follows the completed primary polls.
    *primary, device = data["requests"]
    assert device == (DEVICESTATUS, device_poll, True)
    assert [call for call in primary if AGE_FIELDS[0] not in call[1]] == [
        (PROPERTIES, {}, True), (TREATMENTS, treatment_poll, True),
    ]
    assert [call for call in primary if AGE_FIELDS[0] in call[1]] == [(TREATMENTS, age_poll, True)]


async def test_native_ar2_forecasts_follow_coordinator_refreshes(
    hass, enable_custom_integrations, forecast_site, freezer,
):
    """The frozen native-offset, single-horizon and shifted-clock outcomes occur in HA."""
    freezer.move_to(POLL_TIME)
    url, data = forecast_site
    data["points"] = [(1767874502000, 207), (1767875402000, 231)]
    entry = await _setup(hass, url)
    try:
        assert data["requests"][:2] == [(STATUS, {}, True), (PROBE, {}, True)]
        assert {PROPERTIES, TREATMENTS} <= {path for path, _, _ in data["requests"]}
        first, second = _forecasts(hass, entry)
        assert (first.state, second.state) == ("207", "231")
        assert first.attributes["unit_of_measurement"] == second.attributes["unit_of_measurement"] == "mg/dL"
        assert second.attributes["model"] == "AR2"
        assert _state(hass, entry, "last_reading").state == "2026-01-08T12:00:00+00:00"

        data["points"] = [(1767874500000, 207), (1767875100000, 219)]
        await _refresh(hass, entry, data)
        assert tuple(state.state for state in _forecasts(hass, entry)) == ("207", STATE_UNKNOWN)

        data["points"] = [(1767874800000, 214), (1767875400000, 231)]
        await _refresh(hass, entry, data)
        assert tuple(state.state for state in _forecasts(hass, entry)) == (STATE_UNKNOWN, "231")

        # A seven-minute clock shift makes the old horizon timestamps ineligible.
        # The competing uploaded forecast remains available on the mock server.
        data["mills"] = 1767874020000
        data["points"] = [(1767875822000, 231), (1767874922000, 207)]
        await _refresh(hass, entry, data)
        first, second = _forecasts(hass, entry)
        assert _state(hass, entry, "last_reading").state == "2026-01-08T12:07:00+00:00"
        assert (first.state, second.state) == ("207", "231")
        assert second.attributes["forecast"] == [
            {"time": "2026-01-08T12:22:02+00:00", "mgdl": 207},
            {"time": "2026-01-08T12:37:02+00:00", "mgdl": 231},
        ]
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_released_ar2_off_grid_points_remain_accepted(
    hass, enable_custom_integrations, forecast_site, freezer,
):
    """Baseline difference: H1 expects Unknown at ±60 s, the released client accepts it."""
    freezer.move_to("2026-01-08T12:10:00Z")
    url, data = forecast_site
    data["points"] = [(1767874560000, 207), (1767875340000, 231)]
    entry = await _setup(hass, url)
    try:
        assert tuple(state.state for state in _forecasts(hass, entry)) == ("207", "231")
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
