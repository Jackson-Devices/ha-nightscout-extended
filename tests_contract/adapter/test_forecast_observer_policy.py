"""Exercise the public forecast timestamp observation and its exact policy."""

from datetime import datetime, timedelta

from aiohttp import web
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State
import pytest
from pytest_socket import socket_allow_hosts

from tests_contract.adapter.observe_ha import HomeAssistantObserver
from tests_contract.adapter.test_supplemental_replay import (
    CASES,
    PREPARED,
    SupplementalObserver,
    SupplementalReplay,
    _product_policy,
    _redact,
)


pytest_plugins = ["pytest_homeassistant_custom_component"]
CASE_ID = "F-AR2-MIXED"
NATIVE_ACTUAL = {
    "forecast_15": [207, "mg/dL", None, "available", {}],
    "forecast_30": [231, "mg/dL", None, "available", {}],
}
NATIVE_TIMESTAMPS = {
    "forecast_15": {
        "source_time": "2026-01-08T12:07:00+00:00",
        "forecast_time": "2026-01-08T12:22:02+00:00",
    },
    "forecast_30": {
        "source_time": "2026-01-08T12:07:00+00:00",
        "forecast_time": "2026-01-08T12:37:02+00:00",
    },
}


@pytest.mark.parametrize("role", NATIVE_TIMESTAMPS)
@pytest.mark.parametrize("attribute", ["source_time", "forecast_time"])
def test_forecast_policy_rejects_each_timestamp_only_change(monkeypatch, tmp_path, role, attribute):
    observer = HomeAssistantObserver(None, None, tmp_path)
    _, _, expected, _, _, _, _ = PREPARED[CASE_ID][0]
    states = {
        key: State(f"sensor.{key}", str(NATIVE_ACTUAL[key][0]), attrs)
        for key, attrs in NATIVE_TIMESTAMPS.items()
    }
    monkeypatch.setattr(observer, "_state", states.get)

    def differences():
        return _product_policy(
            CASE_ID, 1, NATIVE_ACTUAL, expected, [], "loaded",
            forecast_timestamps=observer.forecast_timestamps(),
        )

    assert differences() == []
    original = states[role]
    stamp = datetime.fromisoformat(original.attributes[attribute]) + timedelta(seconds=1)
    states[role] = State(original.entity_id, original.state, {**original.attributes, attribute: stamp.isoformat()})

    failures = differences()
    assert len(failures) == 1
    assert f"product.forecast_timestamps.{role}.{attribute}" in failures[0]


def test_forecast_observer_preserves_null_and_missing_attributes(monkeypatch, tmp_path):
    observer = HomeAssistantObserver(None, None, tmp_path)
    states = {
        "forecast_15": State("sensor.forecast_15", STATE_UNKNOWN, {"source_time": None}),
        "forecast_30": State("sensor.forecast_30", STATE_UNAVAILABLE, {"forecast_time": None}),
    }
    monkeypatch.setattr(observer, "_state", states.get)

    assert observer.forecast_timestamps() == {
        "forecast_15": {"source_time": None},
        "forecast_30": {"forecast_time": None},
    }
    del states["forecast_30"]
    assert observer.forecast_timestamps()["forecast_30"] is None


async def test_forecast_observer_keeps_v2_source_when_v3_sgv_differs(
    freezer, hass_storage, tmp_path, socket_enabled,
):
    socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    _, request, expected, _, _, _, _ = PREPARED[CASE_ID][0]
    replay = SupplementalReplay()
    replay.set_step(CASE_ID, CASES[CASE_ID]["steps"][0], request)
    # Change only the v3 stored measurement. The V2 bgnow and AR2 payload
    # remain the pinned originals, with the chosen targets two seconds off grid.
    replay.sgv_versions["sgv-shift"]["date"] = 1767874080000  # 12:08 UTC, not 12:07.
    observer = SupplementalObserver(freezer, hass_storage, tmp_path, replay)
    app = web.Application()
    app.router.add_get("/{path:.*}", replay.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        actual = await observer.observe(url, request)
        assert not replay.errors, _redact(str(replay.errors))
        assert observer.entry_state == "loaded"
        assert observer._sensor_observation("glucose")[:4] == [
            186, "mg/dL", "2026-01-08T12:08:00Z", "available",
        ]
        assert actual == NATIVE_ACTUAL
        assert observer.forecast_timestamps() == NATIVE_TIMESTAMPS
        assert _product_policy(
            CASE_ID, 1, actual, expected, replay.calls, observer.entry_state,
            forecast_timestamps=observer.forecast_timestamps(),
        ) == []
    finally:
        try:
            await observer.close()
        finally:
            await runner.cleanup()
