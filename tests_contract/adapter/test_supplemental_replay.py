"""Nonblind observation of frozen scenarios through the actual HA integration.

Unlike the strict replay, this server supplies the integration's companion
calls and permits its own collection query shape. It does not change the
strict suite or declare wire conformance. Per-step evidence identifies which
fixture replies the integration reached.
"""

import asyncio
from datetime import datetime
from email.utils import format_datetime
import json
from pathlib import Path
from urllib.parse import quote, quote_plus

from aiohttp import web
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_socket import socket_allow_hosts

from custom_components.nightscout_extended.api import (
    NightscoutAuthError,
    NightscoutConnectionError,
)
from tests_contract.adapter.observe_ha import HomeAssistantObserver
from tests_contract.contract import run as frozen
from tests_contract.contract.service import WATCHED_HEADERS, reply


pytest_plugins = ["pytest_homeassistant_custom_component"]
REPLAY_CASES = {
    "A-V3-PUBLIC-VERSION": "version is observable on the wire; no HA state is assumed",
    "A-V3-RENEW": "both frozen reactive and proactive authorization alternatives are available",
    "A-V3-REVOKED": "the old and renewed credentials select their original replies",
    "A-V3-SUBJECT-FAILURES": "legacy fallback is a visible outcome after exchange rejection",
    "A-V3-FORBIDDEN": "the frozen protected 403 is served with its original credential",
    "G-PUBLIC-DELTA": "public legacy computed bgnow/delta; no derived direction is supplied",
    "G-V3-DELTA": "raw v3 SGV rows; absent legacy computed delta is a fixture gap",
    "D-CALCULATED": "raw calculated properties and v1 status; mixed legacy query uses the entry input",
    "D-SETTINGS-ONLY": "legacy v1 status is available to the actual setup and HA state",
    "F-AR2-MIXED": "raw v3 SGV and AR2 with independently sourced v2 bgnow anchor",
    "T-LEGACY": "raw legacy poll and calendar replies are scoped to their HA consumers",
    "T-V3-PAGED": "frozen pages by exact skip; a short first page may terminate a larger native scan",
    "T-FUTURE-OVERLAP": "future data and later-page faults remain distinct replies",
}
PROPERTY_GROUPS = {
    "delta": {"bgnow", "delta"},
    "calculated": {"iob", "cob", "basal"},
    "forecast": {"ar2"},
}


def _data():
    contract = Path(frozen.__file__).parent
    return frozen.read_json(contract / "cases.json"), frozen.read_json(contract / "expected.json")


DATA, ORACLE = _data()
CASES = {case["id"]: case for case in DATA["cases"]}
PREPARED = frozen.preflight(DATA, ORACLE)
_REDACTIONS = {
    value
    for mode in DATA["modes"].values()
    for key, value in mode.items()
    if key != "api"
}
_REDACTIONS.update(
    value.removeprefix("Bearer ")
    for query, headers in DATA["auth"].values()
    for value in (*query.values(), *headers.values())
)


def _redact(value):
    for secret in sorted(_REDACTIONS, key=len, reverse=True):
        for form in {secret, quote(secret, safe=""), quote_plus(secret, safe="")}:
            if form:
                value = value.replace(form, "<fixture-credential>")
    return value


def _wire_response(route, payload):
    return reply(DATA, payload, DATA["routes"][route][2])


def _json_response(body):
    return 200, {}, 0, json.dumps(body).encode("utf-8")


class SupplementalReplay:
    """Serve raw fixture data and audit the integration's real loopback traffic."""

    def __init__(self):
        self.calls = []
        self.errors = []
        self.channel = "entry"
        self.active_bearers = set()

    def set_step(self, step, request):
        self.planned = list(
            dict.fromkeys(
                tuple(item)
                for trace in (step["wire"], *step.get("wire_alternatives", ()))
                for item in trace
            )
        )
        self.request = request
        self.calls.clear()
        self.errors.clear()
        self.reached = set()

    def _auth(self, request):
        query = {key: request.query.getall(key) for key in ("token",) if key in request.query}
        headers = {key: request.headers.getall(key) for key in WATCHED_HEADERS if key in request.headers}
        if not query and not headers:
            return "none"
        for name, (want_query, want_headers) in DATA["auth"].items():
            if query == {key: [value] for key, value in want_query.items()} and headers == {
                key: [value] for key, value in want_headers.items()
            }:
                return name
        credentials = self.request["credentials"]
        if not headers and query == {"token": [credentials.get("subject_token")]}:
            return "entry-query"
        return "unexpected"

    def _planned(self, routes, auth=None):
        candidates = [
            item for item in self.planned
            if item[0] in routes and (auth is None or item[1] == auth)
        ]
        return next((item for item in candidates if item not in self.reached), None) or (
            candidates[-1] if candidates else None
        )

    def _respond_planned(self, source, call):
        route, auth, payload = source
        call["fixture"] = [route, auth, payload]
        call["auth_matches_fixture"] = call["auth"] == auth
        self.reached.add(source)
        return _wire_response(route, payload)

    def _properties(self, names, call):
        call["route"] = "properties"
        if names - {"bgnow", "delta", "direction", "iob", "cob", "ar2", "basal"}:
            return 404, {}, 0, b"{}"
        body = {}
        if self.request["operation"] in {"derived", "forecast", "site"} and "bgnow" in names:
            # This is an independent, unchanged v2 reading fixture. It allows
            # the mixed forecast to expose whether the client uses its v2 anchor.
            body["bgnow"] = DATA["objects"]["delta-mg"]["bgnow"]
            call["anchor"] = "companion:delta-mg.bgnow"
        for group, fields in PROPERTY_GROUPS.items():
            if not names & fields or (names == {"bgnow"}):
                continue
            planned = self._planned({group})
            if planned is None:
                continue
            status, headers, delay, raw = self._respond_planned(planned, call)
            if status != 200:
                return status, headers, delay, raw
            source = json.loads(raw)
            body.update({name: value for name, value in source.items() if name in names})
            call.setdefault("properties", []).append(planned[2])
        call.setdefault("fixture", "companion:properties")
        return _json_response(body)

    def _collection(self, path, request, auth, call):
        version, collection = path.split("/")[2:]
        stem = "entries" if collection == "entries" else "treatments"
        wire = version[-1]
        routes = {stem + wire}
        call["route"] = f"{version}/{stem}"
        if stem == "treatments" and self.channel == "calendar":
            skip = request.query.get("skip", "0")
            routes = {"calendar1"} if wire == "1" else {"calendar3-" + skip}
        if wire == "3":
            if auth == "none" or auth not in self.active_bearers:
                call["fixture"] = "rejected:v3-authorization"
                return 401, {}, 0, b"{}"
            # The v3 serialization of this collection fixture contains no
            # created_at rows. Account for the independent compatibility scan.
            if stem == "treatments" and any(
                key.startswith("created_at$") for key in request.query
            ):
                call["fixture"] = "companion:empty-created-at-scan"
                return _json_response({"status": 200, "result": []})
        source = self._planned(routes, auth)
        if source is not None:
            return self._respond_planned(source, call)
        if any(self._planned({route}) for route in routes):
            call["fixture"] = "rejected:fixture-authorization"
            return 401, {}, 0, b"{}"
        call["fixture"] = "companion:empty-collection"
        return _json_response({"status": 200, "result": []} if wire == "3" else [])

    def _dispatch(self, request, auth, call):
        path = request.path
        if request.method != "GET" or request.can_read_body:
            return 405, {}, 0, b"{}"
        if path.startswith("/api/v2/authorization/request/"):
            call["route"] = "exchange"
            routes = {key for key, (route, _, _) in DATA["routes"].items() if route == path}
            source = self._planned(routes, auth)
            if source is None or auth != "none":
                call["fixture"] = "rejected:unplanned-exchange"
                return 401, {}, 0, b"{}"
            response = self._respond_planned(source, call)
            if response[0] == 200:
                issued = json.loads(response[3])["token"]
                self.active_bearers.update(
                    name for name, (query, headers) in DATA["auth"].items()
                    if not query and headers.get("authorization") == f"Bearer {issued}"
                )
            return response
        if path == "/api/v3/version":
            call["route"] = "v3/version"
            if auth != "none":
                call["fixture"] = "rejected:nonpublic-version"
                return 400, {}, 0, b"{}"
            if self.request["credentials"]["api"] == "v1":
                call["fixture"] = "companion:legacy-no-v3"
                return 404, {}, 0, b"{}"
            source = self._planned({"version"}, "none")
            if source is not None:
                return self._respond_planned(source, call)
            call["fixture"] = "companion:version"
            return _json_response(DATA["objects"]["version"])
        if path.startswith(("/api/v1/", "/api/v2/properties/")) and auth not in {
            "none", "query", "entry-query",
        }:
            call["fixture"] = "rejected:legacy-authorization"
            return 401, {}, 0, b"{}"
        if path in {"/api/v1/status", "/api/v1/status.json"}:
            call["route"] = "v1/status"
            source = self._planned({"status"})
            if source is not None:
                return self._respond_planned(source, call)
            name = "site-mmol" if self.request["units"] == "mmol/L" else "site-mg"
            call["fixture"] = f"companion:{name}"
            return _json_response(DATA["objects"][name])
        if path.startswith("/api/v2/properties/"):
            return self._properties(set(path.rsplit("/", 1)[-1].split(",")), call)
        canonical = path.removesuffix(".json")
        if canonical in {"/api/v1/treatments", "/api/v3/treatments", "/api/v3/entries"}:
            return self._collection(canonical, request, auth, call)
        call["fixture"] = "rejected:unsupported-replay-route"
        return 404, {}, 0, b"{}"

    async def handle(self, request):
        call = {
            "route": "other",
            "method": request.method,
            "consumer": self.channel,
            "auth": self._auth(request),
            "query_keys": sorted({
                key for key in request.query if key.startswith(("date$", "created_at$", "find["))
                or key in {"limit", "skip", "count", "type", "sort$desc", "sort$asc"}
            }),
        }
        self.calls.append(call)
        try:
            status, headers, delay, body = self._dispatch(request, call["auth"], call)
            call["status"] = status
            if delay:
                await asyncio.sleep(delay)
            date = format_datetime(datetime.fromisoformat(self.request["clock"]), usegmt=True)
            return web.Response(
                status=status, headers={"Date": date, **headers},
                body=body, content_type="application/json",
            )
        except Exception as error:
            self.errors.append(_redact(f"{type(error).__name__}: {error}"))
            return web.Response(status=500)


class SupplementalObserver(HomeAssistantObserver):
    def __init__(self, freezer, storage, config_dir, replay):
        super().__init__(freezer, storage, config_dir)
        self.replay = replay

    async def _calendar(self, parameters):
        self.replay.channel = "calendar"
        try:
            return await super()._calendar(parameters)
        except (HomeAssistantError, NightscoutAuthError, NightscoutConnectionError) as error:
            # A failed public service call is observed separately from sensors.
            return {"service_error": type(error).__name__, "detail": _redact(str(error))}
        finally:
            self.replay.channel = "entry"


def _render(event):
    print(_redact(json.dumps(event, sort_keys=True)), flush=True)


@pytest.mark.parametrize("case_id", REPLAY_CASES)
async def test_supplemental_frozen_replay(case_id, freezer, hass_storage, tmp_path, socket_enabled):
    """Keep taking observations after an earlier expected semantic difference."""
    socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    replay = SupplementalReplay()
    observer = None
    differences = []
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", replay.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        for raw_step, (index, request, expected, _, seed, empty_store, lookups) in zip(
            CASES[case_id]["steps"], PREPARED[case_id], strict=True
        ):
            replay.set_step(raw_step, request)
            if observer is None or request["lifecycle"] != "poll":
                if seed is not None or lookups is not None:
                    raise RuntimeError("seeded registry is outside supplemental coverage")
                previous = None
                if observer is not None:
                    if request["lifecycle"] != "fresh" and not empty_store:
                        previous = await observer.dump_state()
                    await observer.close()
                replay.active_bearers.clear()
                observer = SupplementalObserver(freezer, hass_storage, tmp_path, replay)
                if previous is not None:
                    observer.load_state(previous)

            actual = await observer.observe(url, request)
            if replay.errors:
                raise RuntimeError(f"fixture HTTP handler failed: {replay.errors!r}")
            mismatches = []
            if expected.keys() != actual.keys():
                mismatches.append(f"observation fields expected {list(expected)}, got {list(actual)}")
            for key in sorted(expected.keys() & actual.keys()):
                try:
                    frozen.compare(expected[key], actual[key], {}, path=f"observation.{key}")
                except frozen.Mismatch as error:
                    mismatches.append(str(error))
            differences.extend(f"step {index}: {mismatch}" for mismatch in mismatches)
            _render({
                "kind": "supplemental-ha-replay", "case": case_id, "step": index,
                "scope": REPLAY_CASES[case_id], "setup_state": observer.entry_state,
                "lifecycle": request["lifecycle"], "observed": actual,
                "differences": mismatches, "wire": replay.calls,
                "unreplayed_fixture_replies_including_alternatives": [
                    item for item in replay.planned if item not in replay.reached
                ],
            })
    except Exception as error:
        pytest.fail(_redact(f"SUPPLEMENT_HARNESS {type(error).__name__}: {error}"), pytrace=False)
    finally:
        try:
            if observer is not None:
                await observer.close()
        finally:
            await runner.cleanup()
    if differences:
        pytest.fail(_redact("SUPPLEMENT_MISMATCH " + "\n".join(differences)), pytrace=False)
