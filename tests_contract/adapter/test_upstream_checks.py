"""Replay frozen contract cases and establish an upstream-shaped HA control."""

import asyncio
import json
from operator import itemgetter
from pathlib import Path
from urllib.parse import quote, quote_plus

from aiohttp import web
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_socket import socket_allow_hosts

from tests_contract.adapter.observe_ha import FrozenRunnerObserver, HomeAssistantObserver
from tests_contract.contract import run as frozen


pytest_plugins = ["pytest_homeassistant_custom_component"]
CONTRACT = Path(frozen.__file__).parent
DATA = frozen.read_json(CONTRACT / "cases.json")
CASES = frozen.preflight(DATA, frozen.read_json(CONTRACT / "expected.json"))


@pytest.fixture
def loopback_socket(socket_enabled):
    # Use real sockets for the fixture and transport, limited to its interface.
    socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)


def _redact(message):
    values = {
        value for mode in DATA["modes"].values() for key, value in mode.items()
        if key != "api"
    }
    values.update(
        value.removeprefix("Bearer ")
        for query, headers in DATA["auth"].values()
        for value in (*query.values(), *headers.values())
    )
    for value in sorted(values, key=len, reverse=True):
        for form in {value, quote(value, safe=""), quote_plus(value, safe="")}:
            if form:
                message = message.replace(form, "<synthetic-credential>")
    return message


@pytest.mark.parametrize("case_id", CASES)
async def test_frozen_contract(case_id, freezer, hass_storage, tmp_path, loopback_socket):
    """Use unchanged replay and comparison, with one pytest node per case."""
    loop = asyncio.get_running_loop()
    observers = []

    def observer_factory():
        observer = HomeAssistantObserver(freezer, hass_storage, tmp_path)
        observers.append(observer)
        return FrozenRunnerObserver(loop, observer)

    try:
        await asyncio.to_thread(frozen.execute, CASES, [case_id], observer_factory, False)
    except frozen.Mismatch as error:
        actual = [step for observer in observers for step in observer.observations]
        message = f"BASELINE_MISMATCH {error}\nHA observations: {json.dumps(actual, sort_keys=True)}"
        if any(
            "api_secret_hash" in request["credentials"]
            and "api_secret" not in request["credentials"]
            for _, request, *_ in CASES[case_id]
        ):
            message += "\nThe fixture supplies a pre-hashed secret. The released HA entry accepts a cleartext secret."
        pytest.fail(_redact(message), pytrace=False)
    except Exception as error:
        pytest.fail(_redact(f"HARNESS {type(error).__name__}: {error}"), pytrace=False)


@pytest.fixture
async def upstream_replay(loopback_socket):
    """Serve only the routes implemented by the released integration."""
    fixture = {"glucose": 153, "carbs": 23, "insulin": 1.25, "requests": []}

    async def status(request):
        fixture["requests"].append(request.path)
        return web.json_response({"version": "15.0.9", "settings": {"units": "mg/dl"}})

    async def properties(request):
        fixture["requests"].append(request.path)
        return web.json_response({
            "bgnow": {"last": fixture["glucose"], "mills": 1767873600000},
            "direction": {"value": "Flat"},
            "delta": {"mgdl": 0},
        })

    async def treatments(request):
        fixture["requests"].append(request.path)
        return web.json_response([
            {"_id": "dose", "created_at": "2026-01-08T11:30:00Z",
             "eventType": "Correction Bolus", "insulin": fixture["insulin"]},
            {"_id": "meal", "created_at": "2026-01-08T11:40:00Z",
             "eventType": "Carb Correction", "carbs": fixture["carbs"]},
        ])

    app = web.Application()
    app.router.add_get("/api/v1/status.json", status)
    app.router.add_get("/api/v2/properties/bgnow", properties)
    app.router.add_get("/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal", properties)
    app.router.add_get("/api/v1/treatments.json", treatments)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}", fixture
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("transition", ["restart", "upgrade"])
async def test_upstream_setup_and_lifecycle(
    transition, freezer, hass_storage, tmp_path, upstream_replay
):
    """Prove the HA observer can read live state when the baseline gets valid replies."""
    url, fixture = upstream_replay
    request = {
        "clock": "2026-01-08T12:00:00Z",
        "credentials": {},
        "lifecycle": "fresh",
        "operation": "identity",
        "parameters": {
            "calendar": {"start": "2026-01-08T11:00:00Z", "end": "2026-01-08T13:00:00Z"},
        },
    }
    observer = HomeAssistantObserver(freezer, hass_storage, tmp_path)
    try:
        fresh = await observer.observe(url, request)
        assert observer.entry_state == "loaded"
        assert fresh["glucose"][:4] == [153, "mg/dL", "2026-01-08T12:00:00Z", "available"]
        assert fresh["latest_bolus"][0] == 1.25
        assert fresh["latest_carbs"][0] == 23
        assert [event[2] for event in fresh["calendar"][0]] == [
            "Correction Bolus: 1.25 U insulin", "Carb Correction: 23 g carbs",
        ]
        assert fixture["requests"][0] == "/api/v1/status.json"

        fixture.update(glucose=176, carbs=31, insulin=0.5)
        fixture["requests"].clear()
        changed = await observer.observe(url, request | {"lifecycle": "poll"})
        assert "/api/v1/status.json" not in fixture["requests"]
        assert "/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal" in fixture["requests"]
        assert changed["glucose"][0] == 176
        assert changed["latest_bolus"][0] == 0.5
        assert changed["latest_carbs"][0] == 31

        registry = er.async_get(observer.hass)
        glucose = next(row for row in fresh["registry"] if row["role"] == "glucose")
        registry.async_update_entity(
            glucose["entity_id"], name="Kitchen glucose", aliases=["Kitchen reading"],
        )
        reloaded = await observer.observe(url, request | {"lifecycle": "reload"})
        saved = await observer.dump_state()
        first_hass = observer.hass
        entry_id = observer.entry.entry_id
    finally:
        await observer.close()

    # Both transitions feed HA's normal setup the saved version of the entry.
    # The frozen registry oracle carries the separate generation-migration check.
    successor = HomeAssistantObserver(freezer, hass_storage, tmp_path)
    successor.load_state(json.loads(json.dumps(saved)))
    try:
        resumed = await successor.observe(url, request | {"lifecycle": transition})
        assert successor.hass is not first_hass
        assert successor.entry.entry_id == entry_id
        assert successor.entry_state == "loaded"
        row_id = itemgetter("id")
        assert sorted(resumed["registry"], key=row_id) == sorted(reloaded["registry"], key=row_id)
        row = next(row for row in resumed["registry"] if row["role"] == "glucose")
        assert row["id"] == glucose["id"]
        assert row["name"] == "Kitchen glucose"
        assert row["aliases"] == ["Kitchen reading"]
        assert successor.lookup_registry(row["domain"], row["platform"], row["unique_id"]) == row["id"]
        assert resumed["glucose"][0] == 176
    finally:
        await successor.close()
