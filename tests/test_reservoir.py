"""Reservoir selection and acquisition against synthetic Nightscout payloads."""

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import time

import aiohttp
import pytest
from aiohttp import web

PATH = Path(__file__).resolve().parents[1] / "custom_components/nightscout_extended/api.py"
SPEC = importlib.util.spec_from_file_location("ns_reservoir_api", PATH)
api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api
SPEC.loader.exec_module(api)

NOW = datetime(2026, 1, 8, 12, tzinfo=timezone.utc)
DEVICE_A = "openaps://Fixture A"
DEVICE_B = "openaps://Fixture B"
V1 = "/api/v1/devicestatus/"
V3 = "/api/v3/devicestatus"


def _status(identifier, device=DEVICE_A, *, contact="2026-01-08T11:55:00Z", units=8.25):
    return {
        "identifier": identifier,
        "device": device,
        "date": int((NOW - timedelta(seconds=1)).timestamp() * 1000),
        "created_at": "2026-01-08T11:59:59.000Z",
        "pump": {"reservoir": units, "status": {"timestamp": contact}, "clock": NOW.isoformat()},
    }


@pytest.mark.parametrize("elapsed", [-0.001, 0, 3600, 3600.001])
def test_freshness_uses_exact_contact_boundary(elapsed):
    contact = NOW - timedelta(seconds=elapsed)
    row = _status("boundary", contact=contact.isoformat(), units=0)
    reading = api.select_reservoir([row], NOW, DEVICE_A)
    if 0 <= elapsed <= 3600:
        assert reading == api.ReservoirReading(0, contact, DEVICE_A)
    else:
        assert reading is None


@pytest.mark.parametrize("amount", [-1, False, "8.25", float("inf"), float("nan"), None])
def test_invalid_latest_reservoir_never_revives_an_older_amount(amount):
    latest = _status("latest", units=amount)
    earlier = _status("earlier", contact="2026-01-08T11:45:00Z")
    assert api.select_reservoir([latest, earlier], NOW, DEVICE_A) is None


@pytest.mark.parametrize("contact", [None, "", "unknown", "2026-01-08T11:59:59"])
def test_serialization_time_never_supplies_missing_or_invalid_contact(contact):
    assert api.select_reservoir([_status("bad-clock", contact=contact)], NOW, DEVICE_A) is None


def test_selection_uses_exact_label_and_contact_time_with_equal_time_duplicates():
    latest = _status("latest", contact="2026-01-08T12:55:00+01:00")
    duplicate = _status("same", contact="2026-01-08T11:55:00Z")
    earlier = _status("backfill", contact="2026-01-08T11:44:00Z", units=42)
    other = _status("other", DEVICE_B, contact="2026-01-08T11:59:00Z", units=99)
    unrelated = {**_status("battery-only", "phone-uploader"), "pump": {"battery": {"percent": 80}}}
    rows = [earlier, other, duplicate, latest, unrelated]
    assert api.select_reservoir(rows, NOW, DEVICE_A) == api.ReservoirReading(
        8.25, datetime(2026, 1, 8, 11, 55, tzinfo=timezone.utc), DEVICE_A,
    )
    assert api.select_reservoir(rows, NOW, DEVICE_A.lower()) is None
    assert api.select_reservoir(rows, NOW, None) is None
    assert api.select_reservoir([latest, unrelated], NOW, None).uploader == DEVICE_A
    assert api.select_reservoir([latest, {**duplicate, "device": None}], NOW, None) is None
    assert api.select_reservoir([*rows, _status("conflict", units=2)], NOW, DEVICE_A) is None


def test_latest_contact_without_reservoir_suppresses_older_reading():
    latest = _status("no-amount", contact="2026-01-08T11:56:00Z")
    del latest["pump"]["reservoir"]
    assert api.select_reservoir([_status("older"), latest], NOW, DEVICE_A) is None


@pytest.mark.parametrize("v3", [False, True], ids=["v1", "v3"])
def test_device_status_acquisition_is_bounded_and_complete(v3):
    async def run():
        calls = []
        rows = [
            _status("new-upload", contact="2026-01-08T11:40:00Z", units=24),
            _status("earlier-upload", units=0),
            _status("other-device", DEVICE_B, contact="2026-01-08T11:58:00Z", units=99),
        ]
        # Its upload clock is ahead of HA; the pump contact still qualifies.
        rows[1]["created_at"] = "2026-01-08T12:00:30.000Z"
        del rows[1]["date"]  # v3 must also scan old storage with only created_at.
        saturated = False

        async def status(_):
            return web.json_response({"settings": {"units": "mg/dl"}})

        async def props(_):
            return web.json_response({"bgnow": {"last": 155}})

        async def version(_):
            return web.json_response({"status": 200, "result": {"apiVersion": "3.0.5"}})

        async def exchange(_):
            now = int(time())
            return web.json_response({"token": "test-bearer", "iat": now, "exp": now + 3600})

        async def device_status(request):
            query = request.query
            calls.append(request.path)
            if v3:
                assert request.path == V3 and request.headers["Authorization"] == "Bearer test-bearer"
                assert "token" not in query
                field = query["sort$desc"]
                assert field in {"date", "created_at"}
                assert query[f"{field}$gte"] == "2026-01-08T11:00:00.000Z"
                assert query[f"{field}$lte"] == "2026-01-08T12:01:00.000Z"
                assert {"device", "pump.status.timestamp", "pump.reservoir"} <= set(query["fields"].split(","))
                limit, skip = int(query["limit"]), int(query["skip"])
                if limit > 1:
                    return web.json_response({"message": "Parameter limit out of tolerance"}, status=400)
                low = datetime.fromisoformat(query[f"{field}$gte"])
                high = datetime.fromisoformat(query[f"{field}$lte"])
                matching = []
                for row in rows:
                    if field not in row:
                        continue
                    observed = (
                        datetime.fromtimestamp(row[field] / 1000, tz=timezone.utc) if field == "date"
                        else datetime.fromisoformat(row[field])
                    )
                    if low <= observed <= high:
                        matching.append(row)
                return web.json_response({"status": 200, "result": matching[skip:skip + limit]})
            assert request.path == V1 and not request.headers.get("Authorization")
            assert query["find[created_at][$gte]"] == "2026-01-08T11:00:00.000Z"
            assert query["find[created_at][$lte]"] == "2026-01-08T12:01:00.000Z"
            count = int(query["count"])
            assert 3 < count <= 1000
            low = query["find[created_at][$gte]"]
            high = query["find[created_at][$lte]"]
            matching = [row for row in rows if low <= row["created_at"] <= high]
            return web.json_response([rows[0]] * count if saturated else matching)

        app = web.Application()
        app.router.add_get("/api/v1/status.json", status)
        app.router.add_get("/api/v2/properties/bgnow", props)
        app.router.add_get("/api/v3/version", version)
        app.router.add_get("/api/v2/authorization/request/test-subject", exchange)
        app.router.add_get(V3 if v3 else V1, device_status)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, "127.0.0.1", 0)
        await server.start()
        (address, port), = runner.addresses
        try:
            async with aiohttp.ClientSession() as session:
                client = api.NightscoutClient(session, f"http://{address}:{port}", "test-subject" if v3 else None)
                await client.async_get_status()
                assert await client.async_get_reservoir(NOW, DEVICE_A) == api.ReservoirReading(
                    0, datetime(2026, 1, 8, 11, 55, tzinfo=timezone.utc), DEVICE_A,
                )
                assert set(calls) == {V3 if v3 else V1}
                if not v3:
                    saturated = True
                    with pytest.raises(api.NightscoutConnectionError, match="Incomplete"):
                        await client.async_get_reservoir(NOW, DEVICE_A)
        finally:
            await runner.cleanup()

    asyncio.run(run())
