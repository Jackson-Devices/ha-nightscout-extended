"""Nonblind observation of frozen scenarios through the actual HA integration.

Unlike the strict replay, this server supplies the integration's companion
calls and permits its own collection query shape. It does not change the
strict suite or declare wire conformance. Per-step evidence identifies which
fixture replies the integration reached.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import math
from pathlib import Path
from urllib.parse import quote, quote_plus

from aiohttp import web
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_conversion import BloodGlucoseConcentrationConverter
import pytest
from pytest_socket import socket_allow_hosts

from custom_components.nightscout_extended.api import (
    NightscoutAuthError,
    NightscoutConnectionError,
)
from tests_contract.adapter.observe_ha import HomeAssistantObserver
from tests_contract.contract import run as frozen
from tests_contract.contract.service import WATCHED_HEADERS, document, epoch, reply


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
    "F-AR2-MIXED": "raw AR2 and a companion v2 bgnow clock from the phase's shifted input sample",
    "T-LEGACY": "raw legacy poll and calendar replies are scoped to their HA consumers",
    "T-V3-PAGED": "actual bounds, sort and paging over the frozen calendar inventory",
    "T-FUTURE-OVERLAP": "native range behavior; separate broad-window late-page fault probe",
}
PROPERTY_GROUPS = {
    "delta": {"bgnow", "delta"},
    "calculated": {"iob", "cob", "basal"},
    "forecast": {"ar2"},
}
THRESHOLD_KEYS = {
    "bg_high": "bgHigh", "bg_target_top": "bgTargetTop",
    "bg_target_bottom": "bgTargetBottom", "bg_low": "bgLow",
}
CALENDAR_PAGE_CAP = 2
SGV_ROUTES = frozenset({"entries1", "entries3"})


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


def _timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed
    return None


def _range_rows(rows, field, lower, upper, *, inclusive_end, descending):
    bounds = _timestamp(lower), _timestamp(upper)
    if None in bounds:
        return None
    start, end = bounds
    dated = [(when, row) for row in rows if (when := _timestamp(row.get(field))) is not None]
    selected = [
        (when, row) for when, row in dated
        if start <= when and (when <= end if inclusive_end else when < end)
    ]
    if descending:
        selected.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in selected]


class SupplementalReplay:
    """Serve raw fixture data and audit the integration's real loopback traffic."""

    def __init__(self):
        self.calls = []
        self.errors = []
        self.channel = "entry"
        self.active_bearers = set()
        self.exchange_replies = {}
        self.sgv_inventory = {}
        self.sgv_versions = {}
        self.sgv_write_clock = 0

    def set_step(self, case_id, step, request):
        self.planned = list(
            dict.fromkeys(
                tuple(item)
                for trace in (step["wire"], *step.get("wire_alternatives", ()))
                for item in trace
            )
        )
        self.case_id = case_id
        self.request = request
        self.interface_reload = False
        self.calls.clear()
        self.errors.clear()
        self.reached = set()
        self.sgv_writes = self._advance_sgv_inventory()

    def _advance_sgv_inventory(self):
        # These named lists are the fixture's stored inventory for a phase.
        # Only an explicit inventory change writes a mock storage revision.
        snapshots = {
            payload for route, _, payload in self.planned
            if route in SGV_ROUTES and payload in DATA["lists"]
        }
        if not snapshots:
            return 0
        if len(snapshots) != 1:
            raise ValueError("a supplemental phase must specify one SGV inventory")
        snapshot = {}
        for record_id in DATA["lists"][next(iter(snapshots))]:
            row = document(record_id, DATA["records"], "v3")
            if row.get("type") != "sgv":
                continue
            identity = row["identifier"]
            if identity in snapshot and snapshot[identity] != record_id:
                raise ValueError("conflicting SGV representations in one fixture inventory")
            snapshot[identity] = record_id

        updated = {
            key for key, record_id in snapshot.items()
            if self.sgv_inventory.get(key) != record_id
        }
        deleted = self.sgv_inventory.keys() - snapshot.keys()
        if updated or deleted:
            self.sgv_write_clock = max(self.sgv_write_clock + 1, epoch(self.request["clock"]))
        for key in updated:
            row = document(snapshot[key], DATA["records"], "v3")
            self.sgv_versions[key] = {**row, "srvModified": self.sgv_write_clock}
        for key in deleted:
            self.sgv_versions[key] = {
                **self.sgv_versions[key], "isValid": False, "srvModified": self.sgv_write_clock,
            }
        self.sgv_inventory = snapshot
        return len(updated) + len(deleted)

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

    def _site_payload(self):
        source = self._planned({"status"})
        name = source[2] if source else (
            "site-mmol" if self.request["units"] == "mmol/L" else "site-mg"
        )
        body = DATA["objects"][name]
        settings = body["settings"]
        # The corpus stores stable contract keys. Nightscout v1 exposes these
        # same measurement values under its status API's camelCase keys.
        thresholds = {THRESHOLD_KEYS.get(key, key): value
                      for key, value in settings["thresholds"].items()}
        return source, name, {**body, "settings": {**settings, "thresholds": thresholds}}

    @property
    def units(self):
        return self._site_payload()[2]["settings"]["units"]

    def _properties(self, names, call):
        call["route"] = "properties"
        if names - {"bgnow", "delta", "direction", "iob", "cob", "ar2", "basal"}:
            return 404, {}, 0, b"{}"
        body = {}
        if self.case_id == "F-AR2-MIXED" and "bgnow" in names:
            entry = self._planned({"entries3"})
            if entry is None or "sgv-shift" not in DATA["lists"][entry[2]]:
                raise ValueError("the mixed AR2 phase requires the supplied shifted sample")
            sample = DATA["records"]["sgv-shift"]
            body["bgnow"] = {"last": sample["sgv"], "mills": epoch(sample["at"])}
            call["anchor"] = "companion:phase-entry/sgv-shift"
        elif self.request["operation"] in {"derived", "site"} and "bgnow" in names:
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

    def _treatments(self, wire, request, auth, call):
        query = request.query
        calendar = self.channel == "calendar"
        if calendar and any(
            key in query for key in ("duration$gt", "find[duration][$gt]", "find[duration][$type]")
        ):
            return self._maximum_duration(wire, query, auth, call)
        if wire == "1":
            route = "calendar1" if calendar else "treatments1"
            source = self._planned({route}, auth)
            if source is None:
                return self._empty_or_denied({route}, wire, call)
            response = self._respond_planned(source, call)
            if response[0] != 200 or source[2] not in DATA["lists"]:
                return response
            count = int(query["count"])
            rows = _range_rows(
                json.loads(response[3]), "created_at", query.get("find[created_at][$gte]"),
                query.get("find[created_at][$lte]"), inclusive_end=True, descending=False,
            )
            if rows is None or count <= 0:
                return 400, {}, 0, b"{}"
            call.update(limit=count, lower=query.get("find[created_at][$gte]"),
                        upper=query.get("find[created_at][$lte]"), returned=min(len(rows), count))
            return _json_response(rows[:count])

        field = query.get("sort$desc")
        if field not in {"date", "created_at"}:
            return 400, {}, 0, b"{}"
        limit, skip = int(query.get("limit", 0)), int(query.get("skip", 0))
        call.update(range_field=field, limit=limit, skip=skip,
                    lower=query.get(f"{field}$gte"), upper=query.get(f"{field}$lt"))
        if limit <= 0 or skip < 0:
            return 400, {}, 0, b"{}"
        if calendar and field == "date" and limit > CALENDAR_PAGE_CAP:
            # The real Nightscout API rejects an oversized page. The product
            # chooses its own next limit and offsets.
            call["fixture"] = "companion:configured-page-cap"
            return 400, {}, 0, b'{"message":"Parameter limit out of tolerance"}'

        routes = ({route for route, _, _ in self.planned if route.startswith("calendar3-")}
                  if calendar else {"treatments3"})
        sources = [item for item in self.planned if item[0] in routes and item[1] == auth]
        if not sources:
            return self._empty_or_denied(routes, wire, call)
        if field == "created_at":
            # The frozen v3 input corpus stores treatments under date, so the
            # separate true created_at range query also exercises empty pages.
            call["fixture"] = "companion:empty-created-at-scan"
            return _json_response({"status": 200, "result": []})

        faults = [
            item for item in sources if item[2] in DATA["faults"]
            and (not calendar or skip == int(item[0].rsplit("-", 1)[-1]))
        ]
        if faults:
            return self._respond_planned(faults[0], call)
        record_ids = dict.fromkeys(
            record_id for _, _, body in sources for record_id in DATA["lists"].get(body, ())
        )
        # A database inventory contains a storage row once. Discard only
        # repeat references to the same frozen input record, never ID aliases.
        rows = _range_rows(
            [document(record_id, DATA["records"], "v3") for record_id in record_ids],
            field, call["lower"], call["upper"], inclusive_end=False, descending=True,
        )
        if rows is None:
            return 400, {}, 0, b"{}"
        page = rows[skip:skip + limit]
        call.update(fixture="ranged:frozen-collection", returned=len(page),
                    sources=[item[2] for item in sources if item[2] in DATA["lists"]])
        self.reached.update(item for item in sources if item[2] in DATA["lists"])
        return _json_response({"status": 200, "result": page})

    def _maximum_duration(self, wire, query, auth, call):
        prefix = "calendar3-" if wire == "3" else "calendar1"
        records = DATA["records"]
        candidates = {
            record_id for route, credential, payload in self.planned
            if route.startswith(prefix) and credential == auth
            for record_id in DATA["lists"].get(payload, ())
            if type(records[record_id].get("duration")) in (int, float)
        }
        ordered = sorted(candidates, key=lambda key: (-records[key]["duration"], key))
        if wire == "3":
            if query.get("sort$desc") != "duration" or query.get("skip") != "0":
                return 400, {}, 0, b"{}"
            minimum = float(query["duration$gt"])
            selected = [key for key in ordered if records[key]["duration"] > minimum]
            limit = int(query["limit"])
        else:
            limit = int(query["count"])
            before = _timestamp(query.get("find[created_at][$lt]"))
            if before is None:
                return 400, {}, 0, b"{}"
            if query.get("find[duration][$type]") == "number":
                if (query.get("sort[0][0]"), query.get("sort[0][1]")) != ("duration", "desc"):
                    return 400, {}, 0, b"{}"
                selected = ordered
            elif query.get("sort[duration]") in {"desc", "-1"} and "find[duration][$gt]" in query:
                minimum = float(query["find[duration][$gt]"])
                selected = [key for key in ordered if records[key]["duration"] > minimum]
            else:
                return 400, {}, 0, b"{}"
            selected = [key for key in selected if _timestamp(records[key]["at"]) < before]
        rows = [document(key, records, f"v{wire}") for key in selected[:limit]]
        call.update(fixture="ranged:calendar-duration-inventory", range_field="duration",
                    limit=limit, returned=len(rows))
        return _json_response({"status": 200, "result": rows} if wire == "3" else rows)

    def _empty_or_denied(self, routes, wire, call):
        if self._planned(routes) is not None:
            call["fixture"] = "rejected:fixture-authorization"
            return 401, {}, 0, b"{}"
        call["fixture"] = "companion:empty-collection"
        return _json_response({"status": 200, "result": []} if wire == "3" else [])

    def _authorize_entries(self, wire, auth, call):
        if wire == "3" and auth not in self.active_bearers:
            call["fixture"] = "rejected:v3-authorization"
            return None, (401, {}, 0, b"{}")
        routes = {"entries" + wire}
        source = self._planned(routes, auth)
        if source is not None and source[2] in DATA["faults"]:
            return source, self._respond_planned(source, call)
        if source is None and self._planned(routes) is not None:
            call["fixture"] = "rejected:fixture-authorization"
            return None, (401, {}, 0, b"{}")
        return source, None

    def _sgv_range(self, wire, request, auth, call):
        call["route"] = f"v{wire}/entries"
        source, refusal = self._authorize_entries(wire, auth, call)
        if refusal is not None:
            return refusal
        query = request.query
        if wire == "3":
            if query.get("type") != "sgv" or query.get("sort") != "date":
                return 400, {}, 0, b"{}"
            lower, upper = int(query["date$gte"]), int(query["date$lt"])
            limit, skip = int(query["limit"]), int(query["skip"])
        else:
            if query.get("find[type]") != "sgv":
                return 400, {}, 0, b"{}"
            lower, upper = int(query["find[date][$gte]"]), int(query["find[date][$lt]"])
            limit, skip = int(query["count"]), 0
        if lower >= upper or limit <= 0 or skip < 0:
            return 400, {}, 0, b"{}"
        rows = []
        for identity, record_id in self.sgv_inventory.items():
            row = self.sgv_versions[identity] if wire == "3" else document(
                record_id, DATA["records"], "v1",
            )
            stamp = row.get("date")
            if type(stamp) is int and lower <= stamp < upper and row.get("isValid") is not False:
                rows.append((stamp, identity, row))
        rows.sort(key=lambda item: item[:2], reverse=wire == "1")
        page = [row for _, _, row in rows[skip:skip + limit]]
        if source is not None:
            self.reached.add(source)
        call.update(
            fixture="ranged:frozen-SGV-inventory", lower=lower, upper=upper,
            limit=limit, skip=skip, returned=len(page), fixture_writes=self.sgv_writes,
        )
        return _json_response({"status": 200, "result": page} if wire == "3" else page)

    def _sgv_history(self, path, request, auth, call):
        call["route"] = "v3/entries/history"
        source, refusal = self._authorize_entries("3", auth, call)
        if refusal is not None:
            return refusal
        since = int(path.rsplit("/", 1)[-1])
        limit = int(request.query["limit"])
        if since < 0 or limit <= 0:
            return 400, {}, 0, b"{}"
        versions = sorted(
            (row for row in self.sgv_versions.values() if row["srvModified"] > since),
            key=lambda row: (row["srvModified"], row["identifier"]),
        )
        page = versions[:limit]
        if source is not None:
            self.reached.add(source)
        call.update(
            fixture="ranged:frozen-SGV-storage-versions", since=since, limit=limit,
            returned=len(page), fixture_writes=self.sgv_writes,
        )
        return _json_response({"status": 200, "result": page})

    def _sgv_fence(self, auth, call):
        call["route"] = "v3/lastModified"
        if auth not in self.active_bearers:
            call["fixture"] = "rejected:v3-authorization"
            return 401, {}, 0, b"{}"
        # The collection marker is a hint. The server-side fence is its wall
        # clock after the stored changes already visible in SEARCH.
        wall = max(epoch(self.request["clock"]), self.sgv_write_clock)
        hint = max(
            (row["date"] for row in self.sgv_versions.values() if type(row.get("date")) is int),
            default=0,
        )
        call.update(fixture="companion:SGV-change-fence", server_clock=wall)
        return _json_response({
            "status": 200,
            "result": {"srvDate": wall, "collections": {"entries": max(hint, self.sgv_write_clock)}},
        })

    def _collection(self, path, request, auth, call):
        version, collection = path.split("/")[2:]
        stem = "entries" if collection == "entries" else "treatments"
        wire = version[-1]
        routes = {stem + wire}
        call["route"] = f"{version}/{stem}"
        if wire == "3":
            if auth == "none" or auth not in self.active_bearers:
                call["fixture"] = "rejected:v3-authorization"
                return 401, {}, 0, b"{}"
        if stem == "treatments":
            return self._treatments(wire, request, auth, call)
        source = self._planned(routes, auth)
        if source is not None:
            return self._respond_planned(source, call)
        return self._empty_or_denied(routes, wire, call)

    def _dispatch(self, request, auth, call):
        path = request.path
        if request.method != "GET" or request.can_read_body:
            return 405, {}, 0, b"{}"
        if path.startswith("/api/v2/authorization/request/"):
            call["route"] = "exchange"
            routes = {key for key, (route, _, _) in DATA["routes"].items() if route == path}
            source = self._planned(routes, auth)
            if auth == "none" and source is not None:
                response = self._respond_planned(source, call)
            elif auth == "none" and self.interface_reload and path in self.exchange_replies:
                call["fixture"] = "companion:repeat-successful-exchange-after-native-reload"
                response = self.exchange_replies[path]
            elif auth == "none" and self.case_id == "A-V3-PUBLIC-VERSION" and "exchange" in routes:
                call["fixture"] = "companion:exchange-after-public-version"
                response = _wire_response("exchange", "bearer-current")
            else:
                call["fixture"] = "rejected:unplanned-exchange"
                return 401, {}, 0, b"{}"
            if response[0] == 200:
                self.exchange_replies[path] = response
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
                response = self._respond_planned(source, call)
                if response[0] == 200:
                    call["api_version"] = json.loads(response[3])["result"]["apiVersion"]
                return response
            call["fixture"] = "companion:version"
            return _json_response(DATA["objects"]["version"])
        if path.startswith(("/api/v1/", "/api/v2/properties/")) and auth not in {
            "none", "query", "entry-query",
        }:
            call["fixture"] = "rejected:legacy-authorization"
            return 401, {}, 0, b"{}"
        if path in {"/api/v1/status", "/api/v1/status.json"}:
            call["route"] = "v1/status"
            source, name, body = self._site_payload()
            if source is not None:
                self._respond_planned(source, call)
            else:
                call["fixture"] = f"companion:{name}"
            call["projection"] = "nightscout-v1-threshold-field-names"
            return _json_response(body)
        if path.startswith("/api/v2/properties/"):
            return self._properties(set(path.rsplit("/", 1)[-1].split(",")), call)
        canonical = path.removesuffix(".json")
        if canonical == "/api/v1/entries" and "find[date][$lt]" in request.query:
            return self._sgv_range("1", request, auth, call)
        if path == "/api/v3/entries" and "date$lt" in request.query:
            return self._sgv_range("3", request, auth, call)
        if path.startswith("/api/v3/entries/history/"):
            return self._sgv_history(path, request, auth, call)
        if path == "/api/v3/lastModified":
            return self._sgv_fence(auth, call)
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
                key for key in request.query
                if key.startswith(("date$", "created_at$", "find[", "sort[0]["))
                or key in {"limit", "skip", "count", "type", "sort", "sort$desc", "sort[duration]", "duration$gt"}
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


def _product_policy(case_id, index, actual, expected, wire, entry_state):
    """Separate release behavior checks from the unaltered H1 comparisons."""
    failures = []
    acquisition_routes = {"v3/entries", "v3/entries/history"}

    def require(condition, explanation):
        if not condition:
            failures.append(explanation)

    def compare(want, got, field):
        try:
            frozen.compare(want, got, {}, path=f"product.{field}")
        except frozen.Mismatch as error:
            failures.append(str(error))

    def calls(route):
        return [call for call in wire if call["route"] == route]

    def v3_entries():
        return [call for call in wire if call["route"] in acquisition_routes]

    def successful_entries():
        reads = v3_entries()
        return (
            all(call["status"] == 200 for call in reads)
            and [call["route"] for call in reads] in [
                ["v3/entries"],
                ["v3/entries/history"],
                ["v3/entries", "v3/entries/history"],
            ]
        )

    def sensor(role, *, omit_timestamp=False):
        value = actual[role]
        require(value is not None, f"{role} is exposed by HA")
        if value is not None:
            if omit_timestamp:
                compare(expected[role][:2] + expected[role][3:4], value[:2] + value[3:4], role)
            else:
                measured = value[:4]
                if measured[1] == "mmol/L" and type(measured[0]) in (float, int):
                    measured[0] = round(measured[0], 1)
                compare(expected[role][:4], measured, role)

    if case_id == "A-V3-PUBLIC-VERSION":
        require(
            [(c["auth"], c["status"], c.get("api_version")) for c in calls("v3/version")]
            == [("none", 200, DATA["objects"]["version"]["result"]["apiVersion"])],
            "v3 version is observed once without credentials",
        )
        require(entry_state == "loaded", "the version probe permits real HA setup")
    elif case_id in {"A-V3-RENEW", "A-V3-REVOKED"}:
        sensor("glucose")
        auth_trace = [
            ("exchange" if call["route"] == "exchange" else "v3/entries", call["status"])
            for call in wire if call["route"] in acquisition_routes or call["route"] == "exchange"
        ]
        if index == 2:
            reactive = [("v3/entries", 401), ("exchange", 200), ("v3/entries", 200)]
            proactive = [("exchange", 200), ("v3/entries", 200)]
            alternatives = [reactive] if case_id == "A-V3-REVOKED" else [reactive, proactive]
            require(auth_trace in alternatives, "a single renewal is followed by a successful read")
        else:
            require(len(calls("exchange")) == (1 if index == 1 else 0),
                    "the first load exchanges once and the unchanged next poll uses its cached bearer")
    elif case_id == "A-V3-SUBJECT-FAILURES":
        if index < 3:
            compare([401], [c["status"] for c in calls("exchange")], "failed subject exchange")
            require(not v3_entries() and entry_state == "loaded", "legacy proof permits legacy fallback")
            glucose = actual["glucose"]
            require(glucose is not None and glucose[3] == "unknown", "permissive legacy replay has no glucose")
        else:
            sensor("glucose")
            require(successful_entries(), "recovery performs a successful bounded acquisition")
    elif case_id == "A-V3-FORBIDDEN":
        if index == 1:
            compare([403], [c["status"] for c in v3_entries()], "protected denial")
            require(len(calls("exchange")) == 1 and not calls("v1/treatments"),
                    "protected 403 causes neither renewal nor legacy downgrade")
            require(entry_state == "setup_error" and actual["glucose"] is None,
                    "the denied entry does not publish glucose")
        else:
            sensor("glucose")
    elif case_id in {"G-PUBLIC-DELTA", "G-V3-DELTA"}:
        sensor("glucose")
        if actual["glucose"] is not None:
            attributes = actual["glucose"][4]
            if case_id == "G-PUBLIC-DELTA":
                require(not v3_entries(), "public legacy reads its raw V2 computed delta")
                compare((17, 17, -17, None, 10, None, 0)[index - 1],
                        attributes.get("delta"), "legacy delta in mg/dL")
            else:
                require(successful_entries(), "v3 performs a successful bounded acquisition")
                require(attributes.get("delta") is None, "no V2 delta can be used without an agreeing V2 reading")
    elif case_id == "D-CALCULATED":
        for role in ("iob", "cob", "basal"):
            sensor(role, omit_timestamp=True)
        if index == 4:
            require(any(c["status"] == 503 for c in calls("properties")),
                    "the upstream fault makes exposed calculated sensors unavailable")
        else:
            compare(expected["site_settings"], actual["site_settings"], "site settings")
    elif case_id == "D-SETTINGS-ONLY":
        compare(expected["site_settings"], actual["site_settings"], "site settings")
        require(any(c["status"] == 200 for c in calls("v1/status")), "site settings came from native status reads")
    elif case_id == "F-AR2-MIXED":
        # The four input snapshots select nearest points within the released
        # three-minute tolerance. Offset/conflicting points remain in the curve.
        # Step 2 has a ±1ms tie. The released chronological order retains
        # the earlier point, so the +15-minute value is 301, not 302.
        quantities = ((207, 231), (301, 231), (207, 231), (207, 330))[index - 1]
        for role, quantity in zip(("forecast_15", "forecast_30"), quantities, strict=True):
            value = actual[role]
            require(value is not None and value[:2] == [quantity, "mg/dL"] and value[3] == "available",
                    f"{role} must report {quantity} mg/dL from the original v2 curve")
    elif case_id.startswith("T-"):
        insulin, carbs = actual["latest_bolus"], actual["latest_carbs"]
        require(carbs is not None and carbs[:2] == [36, "g"], "the latest completed carbs remain 36 g")
        legacy_intake = _legacy_numeric_string_intake(case_id, index)
        if legacy_intake is not None:
            compare(legacy_intake, insulin, "legacy latest nonzero insulin with a numeric-string input")
        else:
            sensor("latest_bolus")
        calendar_calls = [call for call in wire if call["consumer"] == "calendar"]
        scan = [call for call in calendar_calls if call.get("range_field") == "date" and call["status"] == 200]
        event = actual["calendar"]
        rows = event[0] if isinstance(event, list) and isinstance(event[0], list) else []
        if case_id == "T-FUTURE-OVERLAP":
            start, end = DATA["records"]["future-carbs"]["at"], "2026-01-08T12:50:00Z"
            has_future = any(row[1] == start and row[6] == end for row in rows)
            require(has_future is (index in {1, 2, 3, 6}),
                    "calendar returns the long event only when it overlaps the requested interval")
            if index == 2:
                params = PREPARED[case_id][index - 1][1]["parameters"]["calendar"]
                duration = DATA["records"]["future-carbs"]["duration"]
                lower = _timestamp(params["start"]) - timedelta(minutes=duration)
                require(
                    scan and scan[0]["skip"] == 0
                    and _timestamp(scan[0]["lower"]) == lower
                    and _timestamp(scan[0]["upper"]) == _timestamp(params["end"])
                    and sum(item["returned"] for item in scan) == 1
                    and scan[-1]["returned"] < scan[-1]["limit"]
                    and all(
                        item["status"] == 200 or (
                            item["status"] == 400 and item["fixture"] == "companion:configured-page-cap"
                        )
                        for item in calendar_calls
                    )
                    and all(item.get("skip", 0) < 2 for item in scan),
                    "narrow date scan completes one stored row before the scripted offset-2 fault",
                )
        else:
            starts = {row[1] for row in rows}
            require({"2026-01-08T11:00:00Z", "2026-01-08T11:40:00Z"} <= starts,
                    "range includes both original lower and middle treatments")
            require(sum(row[1] == "2026-01-08T11:50:00Z" for row in rows) == (1 if index < 3 else 0),
                    "the calendar displays the stable bolus once until deletion")
            if case_id == "T-V3-PAGED":
                require(len(scan) >= 3 and scan[-1]["returned"] < scan[-1]["limit"],
                        "a capped calendar scan reads multiple actual pages and reaches a short terminal page")
    return failures


def _fields_different(expected, observed):
    result = {}
    if expected.keys() != observed.keys():
        result["<fields>"] = f"expected fields {list(expected)}, got {list(observed)}"
    for key in sorted(expected.keys() & observed.keys()):
        try:
            frozen.compare(expected[key], observed[key], {}, path=f"observation.{key}")
        except frozen.Mismatch as error:
            result[key] = str(error)
    return result


def _calendar_entity_projection(observation):
    """Account for the released five-minute duration and HA service fields."""
    if observation[3] != "available" or not observation[0]:
        return observation
    rows = []
    for _, start, kind, insulin, carbs, notes, end in observation[0]:
        amounts = []
        if carbs:
            amounts.append(f"{carbs:g} g carbs")
        if insulin:
            amounts.append(f"{insulin:g} U insulin")
        title = f"{kind}: {', '.join(amounts)}" if amounts else kind
        if not amounts and notes:
            title = notes.strip().splitlines()[0][:120]
        default_end = (_timestamp(start) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        rows.append([None, start, title, None, None, notes, end or default_end])
    return [rows, *observation[1:]]


def _legacy_numeric_string_intake(case_id, step):
    """Project the original finite numeric-string sentinel only for an actual V1 poll."""
    scope = CASES[case_id]["steps"][step - 1]["wire"]
    legacy = next((payload for route, _, payload in scope if route == "treatments1"), None)
    if legacy is None:
        return None
    input_id = "invalid-amount"
    if input_id not in DATA["lists"][legacy]:
        raise ValueError("the selected legacy treatment phase lost its original numeric-string input")
    row = DATA["records"][input_id]
    amount = row["insulin"]
    if not isinstance(amount, str):
        raise ValueError("legacy treatment sentinel is no longer a JSON string")
    numeric = float(amount)
    if not math.isfinite(numeric) or not numeric:
        raise ValueError("legacy treatment sentinel is no longer finite and nonzero")
    return [numeric, "U", row["at"], "available", {}]


def _narrow_completed_calendar():
    """Derive the one overlapping event supplied before the unrequested fault."""
    case_id = "T-FUTURE-OVERLAP"
    narrow_step = CASES[case_id]["steps"][1]
    _, request, _, _, _, _, _ = PREPARED[case_id][1]
    parameters = request["parameters"]["calendar"]
    source = next(payload for route, _, payload in narrow_step["wire"] if route == "calendar3-0")
    records = list(dict.fromkeys(DATA["lists"][source]))
    if records != ["future-carbs"]:
        raise ValueError("narrow calendar no longer has the single original storage record")
    row = DATA["records"][records[0]]
    start = _timestamp(row["at"])
    duration = row["duration"]
    if type(duration) not in (int, float):
        raise ValueError("narrow calendar's supplied duration is not a JSON number")
    end = start + timedelta(minutes=duration)
    if not (start < _timestamp(parameters["end"]) and end > _timestamp(parameters["start"])):
        raise ValueError("narrow calendar's original event does not overlap")
    dates = start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")
    event = [records[0], dates[0], row["eventType"], row.get("insulin"),
             row.get("carbs"), row.get("notes"), dates[1]]
    return _calendar_entity_projection([[event], None, None, "available", {}])


def _compatible_expectation(case_id, step, frozen_expected):
    """Derive exact public-interface deltas from pinned inputs and stated policy."""
    compatible = dict(frozen_expected)
    reasons = {}

    def replace(field, value, reason):
        if value != frozen_expected[field]:
            compatible[field] = value
            reasons[field] = reason

    if case_id == "A-V3-PUBLIC-VERSION":
        replace("api_version", None, "capability has wire evidence but no public HA sensor")
    elif case_id == "A-V3-FORBIDDEN" and step == 1:
        replace("glucose", None, "protected denial prevents entity creation")
    elif case_id == "A-V3-SUBJECT-FAILURES" and step < 3:
        replace("glucose", [None, "mg/dL", None, "unknown", {}],
                "proved permissive legacy fallback publishes no glucose value")
    elif case_id.startswith("A-V3-"):
        reading = frozen_expected["glucose"]
        trend = "FortyFiveUp" if case_id in {"A-V3-RENEW", "A-V3-REVOKED"} and step > 1 else "SingleUp"
        replace("glucose", reading[:4] + [{"delta": None, "trend": trend}],
                "v3 reading supplies direction, no agreeing calculated V2 delta was supplied")
    elif case_id in {"G-PUBLIC-DELTA", "G-V3-DELTA"}:
        reading = list(frozen_expected["glucose"])
        if step in {2, 3}:
            native = 180 if step == 2 else 163
            converted = BloodGlucoseConcentrationConverter.convert(native, "mg/dL", "mmol/L")
            reading[0] = {"$near": converted, "absolute": 0.000001}
        if case_id == "G-PUBLIC-DELTA":
            deltas = (17, 17, -17, None, 10, None, 0)
            reading[4] = {"delta": deltas[step - 1], "trend": None}
        else:
            reading[4] = {"delta": None, "trend": "SingleDown" if step == 3 else "SingleUp"}
        replace("glucose", reading, "public HA units and released V2 or V3-agreement delta policy")
    elif case_id == "D-CALCULATED":
        for field in ("iob", "cob", "basal"):
            sense = frozen_expected[field]
            replace(field, [*sense[:2], None, sense[3], {}],
                    "the released HA calculated sensor exposes no source timestamp")
        if step == 4:
            replace("site_settings", None,
                    "the public glucose state does not expose settings while wholly unavailable")
    elif case_id == "F-AR2-MIXED":
        quantities = ((207, 231), (301, 231), (207, 231), (207, 330))[step - 1]
        for role, quantity in zip(("forecast_15", "forecast_30"), quantities, strict=True):
            replace(role, [quantity, "mg/dL", None, "available", {}],
                    "released nearest point with a chronological tie; HA scalar has no point-time attribute")
    elif case_id.startswith("T-"):
        for field in ("latest_bolus", "latest_carbs"):
            reading = frozen_expected[field]
            replace(field, reading[:4] + [{}], "released HA treatment sensors do not expose a source ID")
        legacy_intake = _legacy_numeric_string_intake(case_id, step)
        if legacy_intake is not None:
            replace("latest_bolus", legacy_intake, "V1 still accepts the original finite numeric-string amount")
        if case_id == "T-FUTURE-OVERLAP" and step == 2:
            replace("calendar", _narrow_completed_calendar(),
                    "the actual date scan exhausts one stored row before the scripted offset-2 fault")
        else:
            replace("calendar", _calendar_entity_projection(frozen_expected["calendar"]),
                    "HA service omits raw IDs/amounts and gives durationless events their released length")
    return compatible, reasons


def _release_differences(case_id, step, expected, observed, raw):
    release, reasons = _compatible_expectation(case_id, step, expected)
    failures = _fields_different(release, observed)
    for field in sorted(reasons.keys() - raw.keys()):
        failures[field] = "an expected public-interface delta from the frozen result was not observed"
    return failures, reasons


async def _run_supplemental(case_id, freezer, hass_storage, tmp_path, *, integrated):
    """Run the same actual observer for the frozen differential and compatibility."""
    socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    replay = SupplementalReplay()
    observer = None
    differences = []
    policy_failures = []
    compatibility_failures = []
    last_units = None
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
            replay.set_step(case_id, raw_step, request)
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

            interface_request = request
            if observer.hass is not None and last_units != replay.units:
                # The real integration reads display units at setup, not on an
                # ordinary poll. HA also preserves the existing entity's unit
                # unless the user changes its sensor preference.
                glucose = observer._row("glucose")
                if glucose is None:
                    raise RuntimeError("cannot configure the display unit without a real glucose entity")
                display_unit = "mmol/L" if replay.units == "mmol" else "mg/dL"
                er.async_get(observer.hass).async_update_entity_options(
                    glucose.entity_id, "sensor",
                    {**glucose.options.get("sensor", {}), "unit_of_measurement": display_unit},
                )
                await observer.hass.async_block_till_done()
                replay.interface_reload = True
                interface_request = request | {"lifecycle": "reload"}
            last_units = replay.units
            actual = await observer.observe(url, interface_request)
            if case_id == "D-CALCULATED" and index == 1:
                if actual["basal"] is not None:
                    policy_failures.append("step 1: basal must be disabled by default")
                row = observer._row("basal")
                if row is None or not row.disabled:
                    raise RuntimeError("baseline basal registry row is missing or unexpectedly enabled")
                er.async_get(observer.hass).async_update_entity(row.entity_id, disabled_by=None)
                replay.interface_reload = True
                actual = await observer.observe(url, request | {"lifecycle": "reload"})
            if replay.errors:
                raise RuntimeError(f"fixture HTTP handler failed: {replay.errors!r}")
            mismatches = _fields_different(expected, actual)
            differences.extend(f"step {index}: {mismatch}" for mismatch in mismatches.values())
            policy = _product_policy(case_id, index, actual, expected, replay.calls, observer.entry_state)
            policy_failures.extend(f"step {index}: {failure}" for failure in policy)
            compatibility, declared = {}, {}
            if integrated:
                compatibility, declared = _release_differences(case_id, index, expected, actual, mismatches)
                compatibility_failures.extend(f"step {index}: {failure}" for failure in compatibility.values())
            _render({
                "kind": "supplemental-ha-replay", "case": case_id, "step": index,
                "scope": REPLAY_CASES[case_id], "setup_state": observer.entry_state,
                "lifecycle": request["lifecycle"], "observed": actual,
                "observer_reloaded": replay.interface_reload,
                "sgv_fixture_writes": replay.sgv_writes,
                "differences": mismatches, "wire": replay.calls,
                "product_policy_failures": policy,
                "declared_release_deltas": declared,
                "compatibility_failures": compatibility,
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
    if policy_failures:
        pytest.fail(_redact("SUPPLEMENT_POLICY_MISMATCH " + "\n".join(policy_failures)), pytrace=False)
    if integrated and compatibility_failures:
        pytest.fail(_redact(
            "SUPPLEMENT_COMPATIBILITY_MISMATCH " + "\n".join(compatibility_failures)
        ), pytrace=False)
    if not integrated and differences:
        pytest.fail(_redact("SUPPLEMENT_MISMATCH " + "\n".join(differences)), pytrace=False)


@pytest.mark.parametrize("case_id", REPLAY_CASES)
async def test_supplemental_frozen_replay(case_id, freezer, hass_storage, tmp_path, socket_enabled):
    """Retain every raw mismatch with the unchanged frozen result."""
    await _run_supplemental(case_id, freezer, hass_storage, tmp_path, integrated=False)


@pytest.mark.parametrize("case_id", REPLAY_CASES)
async def test_supplemental_integrated_compatibility(
    case_id, freezer, hass_storage, tmp_path, socket_enabled,
):
    """Check all public results against H1 plus exact, declared release deltas."""
    await _run_supplemental(case_id, freezer, hass_storage, tmp_path, integrated=True)


async def test_supplemental_late_calendar_fault(freezer, hass_storage, tmp_path, socket_enabled):
    """A wide range has enough supplied rows to reach the frozen later-page 503."""
    socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    case_id = "T-FUTURE-OVERLAP"
    _, case_request, _, _, _, _, _ = PREPARED[case_id][0]
    parameters = case_request["parameters"]
    calendar = parameters["calendar"]
    # Use the source inventory's full window. The narrower frozen service
    # request reaches only one dated row even with the duration lookback.
    request = case_request | {
        "parameters": parameters | {
            "calendar": calendar | {"start": calendar["source_start"], "end": calendar["source_end"]},
        },
    }
    broad, failure = CASES[case_id]["steps"][:2]
    fault = next(item for item in failure["wire"] if item[0] == "calendar3-2")
    replay = SupplementalReplay()
    replay.set_step(case_id, broad | {"wire": [*broad["wire"], fault]}, request)
    observer = SupplementalObserver(freezer, hass_storage, tmp_path, replay)
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", replay.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        actual = await observer.observe(url, request)
        _render({"kind": "supplemental-late-calendar-fault", "observed": actual, "wire": replay.calls})
        assert not replay.errors, _redact(str(replay.errors))
        calls = [
            call for call in replay.calls if call["consumer"] == "calendar"
            and call.get("range_field") == "date" and call["status"] in {200, 503}
        ]
        assert [(call["skip"], call["status"]) for call in calls] == [
            (0, 200), (1, 200), (2, 503),
        ]
        assert all(call["returned"] == call["limit"] == 1 for call in calls[:2])
        assert observer.entry_state == "loaded"
        assert actual["calendar"][3] == "available"
        # The public calendar falls back to the successful poll snapshot.
        # Its 11:40 event was outside the two calendar pages actually returned.
        assert any(event[1] == "2026-01-08T11:40:00Z" for event in actual["calendar"][0])
    finally:
        try:
            await observer.close()
        finally:
            await runner.cleanup()
