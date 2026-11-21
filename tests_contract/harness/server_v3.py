"""Compare raw Nightscout v3 responses with the client on a disposable server."""

import asyncio
from http.client import HTTPConnection
import json
import logging
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlencode, urlsplit

from server_calendar import check_calendar, trace_calendar
from server_sgv import prove_v3_sgv


AUTH = "/api/v2/authorization"
ENTRIES = "/api/v3/entries"
TREATMENTS = "/api/v3/treatments"
SUBJECT = "v3-smoke-reader"
ROOT = Path(__file__).resolve().parents[2]


class QualificationError(Exception):
    """A value-free disposable-server failure."""


def request(port, path, *, method="GET", headers=None, body=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status not in (200, 201):
            return response.status, None
        return response.status, json.load(response)
    finally:
        connection.close()


def await_read(get, predicate, phase):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            status, value = get()
            if status == 200 and predicate(value) and time.monotonic() < deadline:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(max(0, min(1, deadline - time.monotonic())))
    raise QualificationError(f"timed out waiting for {phase}")


def require(value, phase):
    if not value:
        raise QualificationError(phase)


def native_subject(port, admin, *, name=SUBJECT, role="readable"):
    headers = {"api-secret": admin}
    endpoint = f"{AUTH}/subjects"
    await_read(lambda: request(port, endpoint, headers=headers), lambda body: isinstance(body, list), "subject administration")
    status, _ = request(port, endpoint, method="POST", headers=headers, body={"name": name, "roles": [role]})
    require(status == 200, "subject creation")
    status, subjects = request(port, endpoint, headers=headers)
    require(status == 200 and isinstance(subjects, list), "subject listing")
    subject = next((row for row in subjects if row.get("name") == name), {})
    access = subject.get("accessToken")
    require(subject.get("roles") == [role] and isinstance(access, str) and access, "disposable subject role")
    status, exchange = request(port, f"{AUTH}/request/{quote(access, safe='')}")
    require(status == 200 and isinstance(exchange, dict) and exchange.get("sub") == name, "subject exchange")
    bearer = exchange.get("token")
    valid_times = isinstance(exchange.get("iat"), int) and isinstance(exchange.get("exp"), int)
    require(isinstance(bearer, str) and bearer and valid_times and 0 < exchange["iat"] < exchange["exp"],
            "bearer response")
    subject_id = subject.get("_id")
    require(isinstance(subject_id, str) and re.fullmatch("[0-9a-f]{24}", subject_id), "disposable subject ID")
    return access, {"Authorization": f"Bearer {bearer}"}, subject_id, exchange["exp"]


def raw_rows(port, path, query, bearer):
    status, body = request(port, f"{path}?{urlencode(query)}", headers=bearer)
    require(status == 200 and isinstance(body, dict) and body.get("status") == 200, f"response from {path}")
    rows = body.get("result")
    require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), f"result from {path}")
    return rows


def prove_server(port, fixture, admin):
    def v3_version(body):
        result = body.get("result") if isinstance(body, dict) else None
        return (
            isinstance(result, dict) and body.get("status") == 200
            and str(result.get("apiVersion", "")).split(".")[0] == "3"
        )

    await_read(lambda: request(port, "/api/v3/version"), v3_version, "v3 discovery")
    access, bearer, subject_id, expires = native_subject(port, admin)
    require(request(port, f"{ENTRIES}?limit=1")[0] in (401, 403), "anonymous entries restriction")
    # Read permission without attempting a write or relying on public default roles.
    denied = f"{AUTH}/debug/check/{quote('api:treatments:create', safe=':')}"
    require(request(port, denied, headers=bearer)[0] in (401, 403), "treatment write restriction")
    glucose = fixture["glucose"]
    millis = fixture["millis"]

    def calculated(body):
        bgnow = body.get("bgnow") if isinstance(body, dict) else None
        return isinstance(bgnow, dict) and (bgnow.get("last"), bgnow.get("mills")) == (glucose, millis)

    query = urlencode({"token": access})
    await_read(lambda: request(port, f"/api/v2/properties/bgnow?{query}"), calculated, "calculated glucose")
    entries = raw_rows(port, ENTRIES, {"type": "sgv", "sort$desc": "date", "limit": 2}, bearer)
    require(bool(entries) and (entries[0].get("sgv"), entries[0].get("date")) == (glucose, millis), "raw SGV")
    for fixture_row in fixture["treatments"]:
        field, expected = fixture_row["field"], fixture_row["document"]
        rows = raw_rows(port, TREATMENTS, {f"{field}$gte": expected[field], "limit": 10}, bearer)
        fields = (field, "eventType", "notes", "carbs", "insulin")
        require(any(all(row.get(key) == expected.get(key) for key in fields) for row in rows),
                f"raw treatment filtered by {field}")
    return access, bearer, subject_id, expires


def revoke_and_prove_denial(port, admin, access, bearer, subject_id, expires):
    # Remove the same subject that passed both collection reads before this check.
    deleted, _ = request(port, f"{AUTH}/subjects/{subject_id}", method="DELETE", headers={"api-secret": admin})
    require(deleted == 200, "native subject deletion")
    exchange, _ = request(port, f"{AUTH}/request/{quote(access, safe='')}")
    entries, _ = request(port, f"{ENTRIES}?limit=1", headers=bearer)
    treatments, _ = request(port, f"{TREATMENTS}?limit=1", headers=bearer)
    unexpired = time.time() < expires
    print(json.dumps({
        "revocation": {
            "deleted_http": deleted, "exchange_http": exchange, "entries_http": entries,
            "treatments_http": treatments, "cached_bearer_unexpired": unexpired,
        },
    }))
    require(unexpired and (exchange, entries, treatments) == (401, 403, 403), "revoked subject raw denial")


async def prove_client(url, fixture, access):
    from aiohttp import ClientSession, ClientTimeout, TraceConfig

    sys.path.insert(0, str(ROOT))
    from custom_components.nightscout_extended.api import NightscoutClient

    observed = set()
    invalid_v3_auth = False
    trace = TraceConfig()

    async def collect(_session, _context, params):
        nonlocal invalid_v3_auth
        path = params.url.path
        if path in (ENTRIES, TREATMENTS):
            invalid_v3_auth |= (
                not params.headers.get("Authorization", "").startswith("Bearer ")
                or "token" in params.url.query or "api-secret" in params.headers
            )
        if params.method != "GET" or params.response.status != 200:
            return
        if path.startswith(f"{AUTH}/request/"):
            observed.add("subject-exchange")
        elif path == "/api/v3/version":
            observed.add("version")
        elif path == ENTRIES:
            observed.add("entries")
        elif path == TREATMENTS:
            for field in ("date", "created_at"):
                if f"{field}$gte" in params.url.query:
                    observed.add(f"treatments-{field}")

    trace.on_request_end.append(collect)
    calendar_trace, calendar_observed = trace_calendar(protected=True)
    async with ClientSession(timeout=ClientTimeout(total=15), trace_configs=[trace, calendar_trace]) as session:
        client = NightscoutClient(session, url, access)
        await client.async_get_status()
        data = await client.async_get_data()
        calendar_passed = await check_calendar(client, calendar_observed, fixture["calendar"], protected=True)
    actual = {
        (row.notes, round(row.time.timestamp() * 1000), row.event_type, row.carbs, row.insulin)
        for row in data.treatments
    }
    expected = {
        (doc["notes"], row["millis"], doc["eventType"], doc.get("carbs"), doc.get("insulin"))
        for row in fixture["treatments"] for doc in (row["document"],)
    }
    result = {
        "wire": sorted(observed),
        "bearer_only": not invalid_v3_auth,
        "glucose": data.glucose == fixture["glucose"],
        "time": bool(data.glucose_time and round(data.glucose_time.timestamp() * 1000) == fixture["millis"]),
        "treatments": actual == expected and len(data.treatments) == len(expected),
    }
    print(json.dumps(result))
    required = {"version", "subject-exchange", "entries", "treatments-date", "treatments-created_at"}
    return calendar_passed and required <= observed and all(
        result[key] for key in ("bearer_only", "glucose", "time", "treatments")
    )


async def prove_revoked_client(url, access):
    from aiohttp import ClientSession, ClientTimeout
    from custom_components.nightscout_extended.api import NightscoutAuthError, NightscoutClient

    outcomes = {}
    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        # Fresh instances make each check independent of in-memory v3/auth mode state.
        for read in ("data", "status"):
            client = NightscoutClient(session, url, access)
            try:
                if read == "data":
                    await client.async_get_data()
                else:
                    await client.async_get_status()
            except NightscoutAuthError as exc:
                outcomes[read] = exc.status
            except Exception as exc:
                outcomes[read] = type(exc).__name__
            else:
                outcomes[read] = "unexpected_success"
    print(json.dumps({"revoked_client": outcomes}))
    return outcomes == {"data": 401, "status": 401}


async def prove_ordered_sgv(url, port, fixture, access, reader_bearer, admin):
    from aiohttp import ClientSession, ClientTimeout
    from custom_components.nightscout_extended.api import NightscoutClient

    for action in ("create", "update", "delete"):
        name = f"api:entries:{action}"
        restricted = f"{AUTH}/debug/check/{quote(name, safe=':')}"
        require(request(port, restricted, headers=reader_bearer)[0] in (401, 403), "read-only SGV subject")

    _, writer_bearer, writer_id, _ = native_subject(port, admin, name="sgv-fixture-writer", role="admin")

    async def mutate(method, path, body):
        status, response = await asyncio.to_thread(request, port, path, method=method, headers=writer_bearer, body=body)
        require(status == (201 if method == "POST" else 200), f"native SGV fixture {method}")
        if method == "POST":
            require(isinstance(response, dict) and isinstance(response.get("identifier"), str)
                    and type(response.get("lastModified")) is int, "native SGV creation receipt")
        return response

    try:
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            client = NightscoutClient(session, url, access)
            await client.async_get_status()
            await prove_v3_sgv(client, fixture, mutate)
    finally:
        status, _ = request(port, f"{AUTH}/subjects/{writer_id}", method="DELETE", headers={"api-secret": admin})
        require(status == 200, "remove disposable SGV fixture writer")


def main(url, fixture_json):
    logging.disable(logging.CRITICAL)
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.netloc != f"127.0.0.1:{parsed.port}" or parsed.path:
        print("v3 qualification requires its disposable loopback server")
        return 2
    fixture = json.loads(fixture_json)
    admin = sys.stdin.readline().strip()
    try:
        access, bearer, subject_id, expires = prove_server(parsed.port, fixture, admin)
    except QualificationError as exc:
        print(f"v3 raw-server qualification failed: {exc}")
        return 2
    except Exception as exc:
        print(f"v3 raw-server qualification failed ({type(exc).__name__})")
        return 2
    print("v3 raw entries and treatments, subject exchange, and read-only authorization passed")
    try:
        if not asyncio.run(prove_client(url, fixture, access)):
            return 1
        asyncio.run(prove_ordered_sgv(url, parsed.port, fixture["ordered_sgv"], access, bearer, admin))
    except QualificationError as exc:
        print(f"v3 ordered SGV fixture failed: {exc}")
        return 2
    except ImportError:
        print("v3 client environment is incomplete")
        return 2
    except Exception as exc:
        print(f"v3 client read failed ({type(exc).__name__})")
        return 1
    try:
        revoke_and_prove_denial(parsed.port, admin, access, bearer, subject_id, expires)
    except QualificationError as exc:
        print(f"v3 raw-server qualification failed: {exc}")
        return 2
    except Exception as exc:
        print(f"v3 raw-server qualification failed ({type(exc).__name__})")
        return 2
    return 0 if asyncio.run(prove_revoked_client(url, access)) else 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
