"""Parsing and auth tests. They load api.py directly, so Home Assistant isn't needed."""

import asyncio
import hashlib
import importlib.util
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp
import pytest
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


SECRET = "averylongapisecret"


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
    digest = hashlib.sha1(SECRET.encode()).hexdigest()
    c, _, data = _run(lambda r: r.headers.get("api-secret") == digest, SECRET)
    assert c.uses_api_secret and data.iob == 1.35


def test_wrong_credential_raises_auth_error():
    try:
        _run(lambda r: False, "wrong")
    except api.NightscoutAuthError:
        return
    raise AssertionError("expected NightscoutAuthError")


def test_redirect_to_other_host_is_refused_and_not_followed():
    seen = []

    async def go():
        evil = web.Application()

        async def capture(request):
            seen.append((request.query.get("token"), request.headers.get("api-secret")))
            return web.json_response({})

        evil.router.add_get("/{p:.*}", capture)
        evil_runner = web.AppRunner(evil); await evil_runner.setup()
        evil_site = web.TCPSite(evil_runner, "127.0.0.1", 0); await evil_site.start()
        evil_port = evil_site._server.sockets[0].getsockname()[1]

        app = web.Application()

        async def redirect(request):
            raise web.HTTPFound(f"http://127.0.0.1:{evil_port}{request.path_qs}")

        app.router.add_get("/{p:.*}", redirect)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as s:
                c = api.NightscoutClient(s, f"http://127.0.0.1:{port}", "readable-abc123")
                try:
                    await c.async_get_status()
                except api.NightscoutConnectionError as err:
                    return str(err)
        finally:
            await runner.cleanup(); await evil_runner.cleanup()

    msg = asyncio.run(go())
    assert msg and "redirected" in msg
    assert "readable-abc123" not in msg  # token in the Location URL is redacted
    assert seen == []  # the other host never received anything


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


_V3_SUBJECT = "unit subject/slash?fixture#encoding"
_V3_ENTRIES = "/api/v3/entries"
_V3_TREATMENTS = "/api/v3/treatments"
_V3_VERSION = "/api/v3/version"
_EXCHANGE = "/api/v2/authorization/request/"


def _v3_result(result):
    return web.json_response({"status": 200, "result": result})


async def _v3_server(handle, exercise, credential=_V3_SUBJECT):
    """Run synthetic routes on loopback with the normal aiohttp client."""
    app = web.Application()
    app.router.add_get("/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        async with aiohttp.ClientSession() as session:
            client = api.NightscoutClient(session, f"http://127.0.0.1:{port}", credential)
            return await exercise(client)
    finally:
        await runner.cleanup()


class _V3Fixture:
    def __init__(self):
        self.exchanges = 0
        self.calls = []
        self.entry_rows = []
        self.treatment_page = lambda query: []
        self.entry_status = 200
        self.reject_first = False
        self.exchange_expiries = []
        self.version_status = 200
        self.exchange_status = 200

    @staticmethod
    def bearer(index):
        return f"synthetic-bearer-{index}"

    async def handle(self, request):
        path = request.path
        self.calls.append(("exchange" if path.startswith(_EXCHANGE) else path, {key: value for key, value in request.query.items() if key != "token"}))
        if path == _V3_VERSION:
            assert "Authorization" not in request.headers and "api-secret" not in request.headers
            assert "token" not in request.query
            if self.version_status != 200:
                return web.Response(status=self.version_status)
            return _v3_result({"apiVersion": "3.0.5"})
        if path.startswith(_EXCHANGE):
            assert request.raw_path.startswith(_EXCHANGE)
            assert bool(request.raw_path.removeprefix(_EXCHANGE) == quote(_V3_SUBJECT, safe=""))
            assert "Authorization" not in request.headers and "api-secret" not in request.headers
            assert not request.query
            self.exchanges += 1
            if self.exchange_status != 200:
                return web.Response(status=self.exchange_status)
            now = int(time.time())
            expiry = self.exchange_expiries.pop(0) if self.exchange_expiries else now + 600
            return web.json_response({"token": self.bearer(self.exchanges), "iat": now - 60, "exp": expiry})
        if path in (_V3_ENTRIES, _V3_TREATMENTS):
            assert "api-secret" not in request.headers and "token" not in request.query
            assert bool(request.headers.get("Authorization") in {
                f"Bearer {self.bearer(n)}" for n in range(1, self.exchanges + 1)
            })
            if self.reject_first and request.headers["Authorization"] == f"Bearer {self.bearer(1)}":
                return web.Response(status=401)
            if path == _V3_ENTRIES:
                if self.entry_status != 200:
                    return web.Response(status=self.entry_status)
                return _v3_result(self.entry_rows)
            result = self.treatment_page(request.query)
            return result if isinstance(result, web.Response) else _v3_result(result)
        assert "Authorization" not in request.headers and "api-secret" not in request.headers
        assert bool(request.query.get("token") == _V3_SUBJECT)
        if path == "/api/v1/status.json":
            return web.json_response({"settings": {"units": "mmol", "thresholds": {"bgLow": 55}}})
        if path == "/api/v1/treatments.json":
            return web.json_response(TREATMENTS)
        assert path.startswith("/api/v2/properties/")
        return web.json_response(PROPS)


async def _configured_data(client):
    status = await client.async_get_status()
    return status, await client.async_get_data()


def test_v3_uses_newest_usable_sgv_and_keeps_computed_property_clock():
    fixture = _V3Fixture()
    anchor = PROPS["bgnow"]["mills"]
    fixture.entry_rows = [
        {"type": "sgv", "date": 4_102_444_800_000, "sgv": 999},
        {"type": "sgv", "date": anchor + 9, "sgv": "nan"},
        {"type": "mbg", "date": anchor + 8, "sgv": 432},
        {"type": "sgv", "date": "not-a-date", "sgv": 551},
        {"type": "sgv", "date": anchor + 1, "sgv": 160, "direction": "SingleDown"},
        {"type": "sgv", "date": anchor, "sgv": 155, "direction": "FortyFiveUp"},
    ]
    status, result = asyncio.run(_v3_server(fixture.handle, _configured_data))
    assert status["settings"] == {"units": "mmol", "thresholds": {"bgLow": 55}}
    assert result.glucose == 160
    assert result.glucose_time == api._ms_to_dt(anchor + 1)
    assert result.direction == "SingleDown" and result.delta is None
    assert result.iob == 1.35 and round(result.cob, 1) == 28.9 and result.basal == 0.29
    assert result.forecast_at(15).mgdl == 169
    assert result.forecast_at(30).mgdl == 175
    assert fixture.exchanges == 1
    assert "/api/v1/treatments.json" not in [path for path, _ in fixture.calls]


def test_v3_glucose_in_mmol_rows_normalizes_to_native_mgdl_and_keeps_matching_delta():
    fixture = _V3Fixture()
    fixture.entry_rows = [{
        "type": "sgv", "date": PROPS["bgnow"]["mills"],
        "sgv": 155 * api.MMOL_PER_MGDL, "units": "mmol/L",
    }]
    _, result = asyncio.run(_v3_server(fixture.handle, _configured_data))
    assert result.glucose == pytest.approx(155)
    assert result.delta == 6 and result.direction == "FortyFiveUp"


_SGV_CLOCK = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)


def _clocked_sgv_poll(monkeypatch, bgnow_offset_ms, measurements):
    """Keep the HA clock fixed while the synthetic server independently publishes bgnow."""
    class LocalClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _SGV_CLOCK.astimezone(tz) if tz else _SGV_CLOCK.replace(tzinfo=None)

    monkeypatch.setattr(api, "datetime", LocalClock)
    origin_ms = int(_SGV_CLOCK.timestamp() * 1000)
    bgnow_ms = origin_ms + bgnow_offset_ms
    props = {
        **PROPS,
        "bgnow": {"mills": bgnow_ms, "last": 155},
        "ar2": {"forecast": {"predicted": [
            {"mills": bgnow_ms + 902_000, "mgdl": 169},
            {"mills": bgnow_ms + 1_802_000, "mgdl": 175},
        ]}},
    }
    rows = [
        {"type": "sgv", "date": origin_ms + offset, "sgv": value, "direction": direction}
        for offset, value, direction in measurements
    ]
    cutoffs = []
    fixture = _V3Fixture()

    async def handle(request):
        if request.path == _V3_ENTRIES:
            cutoff = datetime.fromisoformat(request.query["date$lte"])
            cutoffs.append(cutoff)
            upper = round(cutoff.timestamp() * 1000)
            fixture.entry_rows = sorted(
                (row for row in rows if row["date"] <= upper), key=lambda row: row["date"], reverse=True,
            )
        if request.path.startswith("/api/v2/properties/"):
            return web.json_response(props)
        return await fixture.handle(request)

    _, data = asyncio.run(_v3_server(handle, _configured_data))
    return cutoffs, data


@pytest.mark.parametrize(
    ("offset_ms", "accepted"),
    [(-2_000, True), (0, True), (2_000, True), (59_999, True), (60_000, False)],
)
def test_v3_corroborated_sgv_just_ahead_of_local_clock(monkeypatch, offset_ms, accepted):
    cutoffs, data = _clocked_sgv_poll(monkeypatch, offset_ms, [(offset_ms, 155, "SingleDown")])
    measured = _SGV_CLOCK + timedelta(milliseconds=offset_ms)
    expected_cutoff = max(_SGV_CLOCK, measured) if accepted else _SGV_CLOCK
    assert cutoffs == [expected_cutoff]
    assert data.glucose_time == (measured if accepted else None)
    assert data.glucose == (155 if accepted else None)
    assert data.delta == (6 if accepted else None)
    assert data.direction == ("FortyFiveUp" if accepted else None)
    assert data.forecast_at(15).mgdl == 169
    assert data.forecast_at(30).mgdl == 175


@pytest.mark.parametrize("measurement", [(1_999, 155, "SingleDown"), (2_000, 155.01, "SingleDown")])
def test_v3_unmatched_future_sgv_stays_unknown_inside_widened_query(monkeypatch, measurement):
    cutoffs, data = _clocked_sgv_poll(monkeypatch, 2_000, [measurement])
    assert cutoffs == [_SGV_CLOCK + timedelta(seconds=2)]
    assert data.glucose is None and data.glucose_time is None
    assert data.delta is None and data.direction is None


def test_v3_distinct_nearby_past_sgv_keeps_its_timestamp_and_no_other_delta(monkeypatch):
    cutoffs, data = _clocked_sgv_poll(
        monkeypatch, -2_000, [(-2_000, 155, "FortyFiveUp"), (-1_999, 155, "SingleDown")],
    )
    assert cutoffs == [_SGV_CLOCK]
    assert data.glucose == 155
    assert data.glucose_time == _SGV_CLOCK - timedelta(milliseconds=1_999)
    assert data.direction == "SingleDown"
    assert data.delta is None


@pytest.mark.parametrize("missing_horizon,remaining,expected", [(15, 30, 175), (30, 15, 169)])
def test_v3_forecast_horizons_are_independent_of_selected_sgv(missing_horizon, remaining, expected):
    fixture = _V3Fixture()
    anchor = PROPS["bgnow"]["mills"]
    fixture.entry_rows = [{"type": "sgv", "date": anchor + 600_000, "sgv": 151}]
    when = anchor + remaining * 60_000 + 2_000

    async def handle(request):
        if request.path.startswith("/api/v2/properties/") and "," in request.path:
            return web.json_response({
                **PROPS,
                "ar2": {"forecast": {"predicted": [{"mills": when, "mgdl": expected}]}},
            })
        return await fixture.handle(request)

    _, result = asyncio.run(_v3_server(handle, _configured_data))
    assert result.forecast_at(missing_horizon) is None
    assert result.forecast_at(remaining).mgdl == expected


@pytest.mark.parametrize("version,exchange", [(404, 200), (405, 200), (200, 404), (200, 401)])
def test_absent_capability_or_initial_exchange_rejection_preserves_proven_legacy(version, exchange):
    fixture = _V3Fixture()
    fixture.version_status = version
    fixture.exchange_status = exchange
    _, result = asyncio.run(_v3_server(fixture.handle, _configured_data))
    assert result.glucose == 155
    assert [item.uid for item in result.treatments] == ["t1", "t2", "t3", "t4", "t5"]
    paths = [path for path, _ in fixture.calls]
    assert "/api/v1/treatments.json" in paths
    assert _V3_ENTRIES not in paths and _V3_TREATMENTS not in paths


@pytest.mark.parametrize("version,exchange", [(401, 200), (403, 200), (200, 403)])
def test_capability_auth_errors_stay_attributable(version, exchange):
    fixture = _V3Fixture()
    fixture.version_status = version
    fixture.exchange_status = exchange
    with pytest.raises(api.NightscoutAuthError) as err:
        asyncio.run(_v3_server(fixture.handle, _configured_data))
    assert ("/api/v3/version" if version != 200 else "authorization exchange") in str(err.value)


def test_blank_and_stored_secret_skip_capability():
    for credential, stored in [(None, False), (SECRET, True)]:
        async def handle(request):
            assert request.path.startswith(("/api/v1/", "/api/v2/properties/"))
            assert "token" not in request.query and "Authorization" not in request.headers
            assert bool("api-secret" in request.headers) == stored
            if request.path == "/api/v1/status.json":
                return web.json_response({"settings": {}})
            if request.path == "/api/v1/treatments.json":
                return web.json_response([])
            return web.json_response(PROPS)

        async def exercise(client):
            client.set_uses_api_secret(stored)
            return await _configured_data(client)

        _, result = asyncio.run(_v3_server(handle, exercise, credential))
        assert result.glucose == 155


def test_secret_classification_retries_when_only_protected_legacy_read_denies_query():
    async def handle(request):
        if request.path == "/api/v1/status.json":
            return web.json_response({"settings": {}})
        assert request.path.startswith("/api/v2/properties/")
        digest = hashlib.sha1(SECRET.encode()).hexdigest()
        return web.json_response(PROPS) if request.headers.get("api-secret") == digest else web.Response(status=401)

    async def exercise(client):
        await client.async_get_status()
        assert client.uses_api_secret

    asyncio.run(_v3_server(handle, exercise, SECRET))


@pytest.mark.parametrize("status", [401, 403, 503])
def test_selected_v3_denied_or_unavailable_reads_do_not_downgrade(status):
    fixture = _V3Fixture()
    fixture.entry_status = status
    error = api.NightscoutConnectionError if status == 503 else api.NightscoutAuthError
    with pytest.raises(error, match=rf"HTTP {status}.*entries"):
        asyncio.run(_v3_server(fixture.handle, _configured_data))
    assert fixture.exchanges == (2 if status == 401 else 1)
    assert "/api/v1/treatments.json" not in [path for path, _ in fixture.calls]


@pytest.mark.parametrize("envelope", [{}, {"status": 403, "result": []}, {"status": 200, "result": {}}])
def test_v3_requires_the_collection_envelope(envelope):
    fixture = _V3Fixture()

    async def handle(request):
        if request.path == _V3_ENTRIES:
            return web.json_response(envelope)
        return await fixture.handle(request)

    with pytest.raises(api.NightscoutConnectionError, match="entries"):
        asyncio.run(_v3_server(handle, _configured_data))
    assert fixture.exchanges == 1


def test_v3_401_uses_one_new_bearer_for_concurrent_poll_and_calendar():
    fixture = _V3Fixture()
    fixture.reject_first = True

    async def exercise(client):
        await client.async_get_status()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        await asyncio.gather(client.async_get_data(), client.async_get_treatments(start, end))
        await client.async_get_data()

    asyncio.run(_v3_server(fixture.handle, exercise))
    assert fixture.exchanges == 2


def test_initially_expired_bearer_cannot_be_used_for_protected_requests():
    fixture = _V3Fixture()
    fixture.exchange_expiries[:] = [int(time.time()) - 1]
    _, result = asyncio.run(_v3_server(fixture.handle, _configured_data))
    assert result.glucose is None
    assert fixture.exchanges == 2


def test_v3_treatments_page_both_time_representations_and_refresh_without_stale_ids():
    fixture = _V3Fixture()
    initial = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    start_ms = int(initial.timestamp() * 1000)
    first = {"_id": "native-one", "identifier": "v3-one", "date": start_ms, "eventType": "Meal Bolus", "carbs": 22}
    same_time = {"identifier": "distinct", "date": start_ms, "eventType": "Meal Bolus", "insulin": 1}
    older_origin = {"_id": "legacy", "created_at": "2026-01-01T02:00:00.000Z", "eventType": "Note", "notes": "older upload"}
    has_both = {"_id": "native-one", "identifier": "v3-one", "date": start_ms, "created_at": "2026-01-01T00:00:00.000Z", "eventType": "Meal Bolus", "carbs": 22}
    end_boundary = {"identifier": "after-end", "date": int(end.timestamp() * 1000)}
    edited = {**first, "carbs": 28, "srvModified": start_ms + 4242}
    tombstone = {**same_time, "isValid": False, "srvModified": start_ms + 7000}
    current = [False]

    def treatments(query):
        date_query = "date$gte" in query
        key = "date" if date_query else "created_at"
        assert query["sort$desc"] == key
        assert query[key + "$gte"] == "2026-01-01T00:00:00.000Z"
        assert query[key + "$lt"] == "2026-01-02T00:00:00.000Z"
        if date_query:
            rows = [edited, tombstone, end_boundary] if current[0] else [first, same_time, end_boundary]
        else:
            rows = [older_origin, has_both]
        skip, limit = int(query["skip"]), int(query["limit"])
        return rows[skip:skip + limit]

    fixture.treatment_page = treatments

    async def exercise(client):
        await client.async_get_status()
        first_poll = await client.async_get_treatments(initial, end, limit=2)
        current[0] = True
        return first_poll, await client.async_get_treatments(initial, end, limit=2)

    first_poll, second_poll = asyncio.run(_v3_server(fixture.handle, exercise))
    assert {t.uid for t in first_poll} == {"native-one", "distinct", "legacy"}
    assert {t.uid for t in second_poll} == {"native-one", "legacy"}
    assert next(t for t in second_poll if t.uid == "native-one").carbs == 28
    assert next(t for t in second_poll if t.uid == "legacy").time == initial.replace(hour=2)


@pytest.mark.parametrize("failure", ["HTTP", "non-progress"])
def test_v3_treatments_never_return_a_partial_later_page(failure):
    fixture = _V3Fixture()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    item = {"identifier": "repeated", "date": int(start.timestamp() * 1000), "notes": "synthetic"}

    def page(query):
        if failure == "HTTP" and int(query["skip"]) > 0:
            return web.Response(status=503)
        return [item]

    fixture.treatment_page = page

    async def exercise(client):
        await client.async_get_status()
        await client.async_get_treatments(start, end, limit=1)

    with pytest.raises(api.NightscoutConnectionError, match="treatments"):
        asyncio.run(_v3_server(fixture.handle, exercise))


def test_v3_exchange_redirect_never_follows_or_surfaces_credential_path():
    fixture = _V3Fixture()
    seen = []

    async def foreign(request):
        seen.append(True)
        return web.json_response({})

    async def exercise_foreign(foreign_client):
        async def handle(request):
            if request.path.startswith(_EXCHANGE):
                raise web.HTTPFound(f"{foreign_client.url}{request.raw_path}")
            return await fixture.handle(request)

        async def exercise_source(client):
            await client.async_get_status()

        with pytest.raises(api.NightscoutConnectionError, match="authorization exchange.*redirected") as err:
            await _v3_server(handle, exercise_source)
        message = str(err.value)
        assert bool(_V3_SUBJECT not in message and quote(_V3_SUBJECT, safe="") not in message)
        assert _EXCHANGE not in message

    asyncio.run(_v3_server(foreign, exercise_foreign))
    assert seen == []
