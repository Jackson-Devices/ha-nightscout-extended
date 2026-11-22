"""Parsing and auth tests. They load api.py directly, so Home Assistant isn't needed."""

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location(
    "ns_api", ROOT / "custom_components" / "nightscout_extended" / "api.py"
)
api = importlib.util.module_from_spec(spec)
sys.modules["ns_api"] = api  # dataclasses need the module registered
spec.loader.exec_module(api)

PROPS = json.loads((Path(__file__).parent / "fixtures" / "properties.json").read_text())


def test_parse_full_response():
    d = api.parse_properties(PROPS)
    assert d.glucose == 155
    assert d.glucose_time == datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
    assert d.delta == 6
    assert d.direction == "FortyFiveUp"
    assert d.iob == 1.35 and d.iob_source == "Care Portal"
    assert round(d.cob, 1) == 28.9
    assert d.basal == 0.29
    assert len(d.forecast) == 6
    assert d.forecast_at(15).mgdl == 169
    assert d.forecast_at(30).mgdl == 175


def test_forecast_too_far_returns_none():
    d = api.parse_properties(PROPS)
    assert d.forecast_at(60) is None


def test_missing_plugins_are_none():
    d = api.parse_properties({"bgnow": PROPS["bgnow"]})
    assert d.glucose == 155
    assert d.iob is None and d.cob is None and d.basal is None
    assert d.forecast == [] and d.forecast_at(15) is None


def test_garbage_values_dont_crash():
    d = api.parse_properties({"iob": {"iob": "n/a"}, "cob": {"cob": True}, "ar2": {"forecast": {"predicted": [{"mills": None}]}}})
    assert d.iob is None and d.cob is None and d.forecast == []


# RFC 3174 section 7.3 TEST1 (https://www.rfc-editor.org/rfc/rfc3174#section-7.3).
LEGACY_WIRE_INPUT = "abc"
LEGACY_WIRE_SHA1 = "a9993e364706816aba3e25717850c26c9cd0d89d"


async def _serve(handler_check):
    app = web.Application()

    async def guarded(request):
        if not handler_check(request):
            return web.json_response({"status": 401}, status=401)
        if request.path == "/api/v1/status.json":
            return web.json_response({"version": "15.0.9", "settings": {"units": "mg/dl"}})
        if request.path == "/api/v1/treatments.json":
            return web.json_response([])
        return web.json_response(PROPS)

    app.router.add_get("/api/v1/status.json", guarded)
    app.router.add_get("/api/v2/properties/{names}", guarded)
    app.router.add_get("/api/v1/treatments.json", guarded)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _run(check, credential):
    async def go():
        runner, url = await _serve(check)
        try:
            async with aiohttp.ClientSession() as s:
                c = api.NightscoutClient(s, url, credential)
                status = await c.async_get_status()
                data = await c.async_get_data()
                return c, status, data
        finally:
            await runner.cleanup()
    return asyncio.run(go())


def test_token_auth():
    c, status, data = _run(lambda r: r.query.get("token") == "readable-abc123", "readable-abc123")
    assert not c.uses_api_secret and status["version"] == "15.0.9" and data.glucose == 155


def test_api_secret_fallback():
    c, _, data = _run(
        lambda request: request.headers.get("api-secret") == LEGACY_WIRE_SHA1,
        LEGACY_WIRE_INPUT,
    )
    assert c.uses_api_secret and data.iob == 1.35


def test_wrong_credential_raises_auth_error():
    try:
        _run(lambda r: False, "wrong")
    except api.NightscoutAuthError:
        return
    raise AssertionError("expected NightscoutAuthError")


def test_redirect_to_other_host_is_refused_and_not_followed():
    redaction_sentinel = "readable-abc123"
    sink_path = "/redirect-sink"
    sink_hits = 0

    async def go():
        async def count_hit(_request):
            nonlocal sink_hits
            sink_hits += 1
            return web.json_response({})

        sink = web.Application()
        sink.router.add_get(sink_path, count_hit)
        sink_runner = web.AppRunner(sink, access_log=None)
        await sink_runner.setup()
        try:
            sink_site = web.TCPSite(sink_runner, "127.0.0.1", 0)
            await sink_site.start()
            sink_port = sink_runner.addresses[0][1]
            location = (
                f"http://127.0.0.1:{sink_port}{sink_path}"
                f"?token={redaction_sentinel}"
            )

            async def redirect(_request):
                raise web.HTTPFound(location)

            app = web.Application()
            app.router.add_get("/{p:.*}", redirect)
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                port = runner.addresses[0][1]
                async with aiohttp.ClientSession() as session:
                    client = api.NightscoutClient(
                        session, f"http://127.0.0.1:{port}", redaction_sentinel
                    )
                    try:
                        await client.async_get_status()
                    except api.NightscoutConnectionError as err:
                        return str(err)
            finally:
                await runner.cleanup()
        finally:
            await sink_runner.cleanup()

    msg = asyncio.run(go())
    if not msg or "redirected" not in msg:
        raise AssertionError("The expected redirect refusal was not reported.")
    if redaction_sentinel in msg:
        raise AssertionError("The redirect error exposed the redaction sentinel.")
    assert sink_hits == 0


def test_errors_never_contain_the_credential():
    async def go():
        async with aiohttp.ClientSession() as s:
            c = api.NightscoutClient(s, "http://127.0.0.1:1", "readable-abc123")
            try:
                await c.async_get_status()
            except api.NightscoutConnectionError as err:
                return str(err)

    msg = asyncio.run(go())
    assert msg and "readable-abc123" not in msg


TREATMENTS = json.loads((Path(__file__).parent / "fixtures" / "treatments.json").read_text())


def test_parse_treatments():
    ts = api.parse_treatments(TREATMENTS)
    assert [t.uid for t in ts] == ["t1", "t2", "t3", "t4", "t5"]  # broken and garbage entries skipped
    assert ts[0].summary == "Meal Bolus: 30 g carbs"
    assert ts[1].summary == "Meal Bolus: 2.5 U insulin"
    assert ts[2].summary == "Correction Bolus: 0.5 U insulin"
    assert ts[3].summary.startswith("Planned meal: Dinner") and ts[3].carbs is None
    assert ts[4].summary == "Temporary Target" and ts[4].duration_min == 60


def test_last_treatment_ignores_future_and_zero():
    d = api.NightscoutData(treatments=api.parse_treatments(TREATMENTS))
    at_17 = datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc)
    assert d.last_treatment("carbs", at_17).uid == "t1"
    assert d.last_treatment("insulin", at_17).uid == "t3"
    before = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    assert d.last_treatment("carbs", before) is None


def test_treatments_query_and_auth():
    seen = {}

    async def go():
        app = web.Application()

        async def treatments(request):
            seen.update(request.query)
            return web.json_response(TREATMENTS)

        app.router.add_get("/api/v1/treatments.json", treatments)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as s:
                c = api.NightscoutClient(s, f"http://127.0.0.1:{port}", "readable-abc123")
                return await c.async_get_treatments(
                    datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)
                )
        finally:
            await runner.cleanup()

    ts = asyncio.run(go())
    assert len(ts) == 5
    assert seen["find[created_at][$gte]"] == "2026-01-01T00:00:00.000Z"
    assert seen["find[created_at][$lte]"] == "2026-01-02T00:00:00.000Z"
    assert seen["token"] == "readable-abc123"
