"""Replay controlled Nightscout reads through three actual HA integration paths.

Use the QA checkout's pinned HA Python environment. The upstream and candidate
arguments name checkout roots. Run with --case SCENE/MODE to select one row.
Exit 0 denotes success, 1 a contract mismatch, and 2 a harness error.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from importlib import import_module, metadata
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile


DOMAIN = "nightscout_extended"
COMPONENT = Path("custom_components") / DOMAIN
URL = "https://three-way.example.invalid"
HOST = "three-way.example.invalid"
TITLE = "Three Way"
CLOCK = "2026-01-08T12:10:00+00:00"
SCENES = ("aligned", "newer_raw")
MODES = ("upstream_legacy", "candidate_legacy", "candidate_legacy_denied", "candidate_v3")
CASES = tuple(f"{scene}/{mode}" for scene in SCENES for mode in MODES)
STATUS = "/api/v1/status.json"
BGNOW = "/api/v2/properties/bgnow"
PROPERTIES = "/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal"
VERSION = "/api/v3/version"
EXCHANGE_PREFIX = "/api/v2/authorization/request/"
V1_ENTRIES = "/api/v1/entries.json"
V3_ENTRIES = "/api/v3/entries"
HISTORY_PREFIX = f"{V3_ENTRIES}/history/"
FENCE = "/api/v3/lastModified"
V1_EMPTY = {"/api/v1/treatments.json": "v1.treatments", "/api/v1/devicestatus/": "v1.devicestatus"}
V3_EMPTY = {"/api/v3/treatments": "v3.treatments", "/api/v3/devicestatus": "v3.devicestatus"}
SENSOR_ATTRIBUTES = {
    "glucose": ("unit_of_measurement", "direction", "delta"),
    "glucose_change": ("unit_of_measurement",),
    "glucose_trend": ("unit_of_measurement",),
    "last_reading": ("unit_of_measurement",),
    "forecast_in_15_minutes": ("unit_of_measurement", "source_time", "forecast_time"),
    "forecast_in_30_minutes": ("unit_of_measurement", "source_time", "forecast_time", "forecast", "model"),
}


def millis(iso_time):
    return int(datetime.fromisoformat(iso_time).timestamp() * 1000)


def properties():
    """Use the public paired HA fixture's v2 schema and AR2 sample."""
    return {
        "bgnow": {"last": 153, "mills": 1767873600000},
        "delta": {"mgdl": 7},
        "direction": {"value": "FortyFiveUp"},
        "ar2": {"forecast": {"predicted": [
            {"mills": 1767874502000, "mgdl": 207},
            {"mills": 1767875402000, "mgdl": 231},
        ]}},
    }


def raw_records(scene):
    records = [("111111111111111111111111", "2026-01-08T12:00:00+00:00", 153)]
    if scene == "newer_raw":
        records.append(("222222222222222222222222", "2026-01-08T12:07:00+00:00", 162))
    return [
        {"identifier": identity, "date": millis(when), "sgv": glucose, "type": "sgv",
         "direction": "FortyFiveUp", "srvModified": millis(when) + 1000}
        for identity, when, glucose in records
    ]


class FixtureSite:
    """Serve only the invented site at the HA aiohttp fixture boundary."""

    def __init__(self, scene, mode):
        self.records = raw_records(scene)
        self.mode = mode
        self.subject = secrets.token_hex(16)
        self.bearer = secrets.token_hex(24)
        self.served = []

    @staticmethod
    def wrapped(result):
        return {"status": 200, "result": result}

    def response(self, url):
        path, query = url.path, url.query
        if path == STATUS:
            return "v1.status", 200, {"settings": {"customTitle": TITLE, "units": "mg/dl"}}
        if path == BGNOW:
            return "v2.bgnow", 200, {"bgnow": properties()["bgnow"]}
        if path == PROPERTIES:
            return "v2.properties", 200, properties()
        if path == VERSION:
            if self.mode == "candidate_v3":
                return "v3.version", 200, self.wrapped({"apiVersion": "3.0.5"})
            return "v3.version", 404, {}
        if path.startswith(EXCHANGE_PREFIX):
            if self.mode != "candidate_v3" or path != f"{EXCHANGE_PREFIX}{self.subject}":
                return "v2.exchange", 401, {}
            issued = millis(CLOCK) // 1000
            return "v2.exchange", 200, {"token": self.bearer, "iat": issued, "exp": issued + 3600}
        if path == V1_ENTRIES:
            if self.mode == "candidate_legacy_denied":
                return "v1.entries", 403, {}
            start = int(query["find[date][$gte]"])
            end = int(query["find[date][$lt]"])
            count = int(query["count"])
            if query["find[type]"] != "sgv" or start >= end or count <= 0:
                return "v1.entries", 400, {}
            # V1 records expose storage _id and are returned newest first.
            return "v1.entries", 200, [
                {"_id": row["identifier"], "date": row["date"], "sgv": row["sgv"],
                 "type": row["type"], "direction": row["direction"]}
                for row in reversed(self.records) if start <= row["date"] < end
            ][:count]
        if path == V3_ENTRIES:
            start, end = int(query["date$gte"]), int(query["date$lt"])
            skip, limit = int(query["skip"]), int(query["limit"])
            if query["type"] != "sgv" or query["sort"] != "date" or start >= end or skip < 0 or limit <= 0:
                return "v3.entries.search", 400, {}
            rows = [row for row in self.records if start <= row["date"] < end]
            return "v3.entries.search", 200, self.wrapped(rows[skip:skip + limit])
        if path == FENCE:
            newest = self.records[-1]["srvModified"]
            return "v3.last_modified", 200, self.wrapped({
                "srvDate": millis(CLOCK), "collections": {"entries": newest},
            })
        if path.startswith(HISTORY_PREFIX):
            boundary = int(path.removeprefix(HISTORY_PREFIX))
            limit = int(query["limit"])
            rows = [row for row in self.records if row["srvModified"] > boundary]
            return "v3.entries.history", 200, self.wrapped(rows[:limit])
        if path in V1_EMPTY:
            return V1_EMPTY[path], 200, []
        if path in V3_EMPTY:
            return V3_EMPTY[path], 200, self.wrapped([])
        return "unexpected", 404, {}

    async def handle(self, method, url, _data):
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

        label = "invalid_request"
        try:
            label, status, body = self.response(url)
        except (KeyError, TypeError, ValueError):
            status, body = 400, {}
        entry = {"route": label, "status": status}
        if label in {"v1.entries", "v3.entries.search", "v3.entries.history"} and status == 200:
            rows = body if isinstance(body, list) else body["result"]
            entry["measurements"] = [[row["date"], row["sgv"]] for row in rows]
        if label == "v2.properties":
            entry["bgnow"] = [body["bgnow"]["mills"], body["bgnow"]["last"]]
        self.served.append(entry)
        return AiohttpClientMockResponse(method, url, status=status, json=body)

    def wire_evidence(self, mocker):
        # The transport mock records the effective headers and query. Only
        # authentication validity and fixed route labels leave the process.
        for entry, (method, url, _, headers) in zip(self.served, mocker.mock_calls, strict=True):
            headers = {name.lower(): value for name, value in (headers or {}).items()}
            label = entry["route"]
            entry["method"] = method.upper()
            entry["fixture_origin"] = url.scheme == "https" and url.host == HOST and url.port == 443
            if label in {"v3.version", "v2.exchange"}:
                authentic = "authorization" not in headers and "token" not in url.query
            elif label.startswith("v3."):
                authentic = headers.get("authorization") == f"Bearer {self.bearer}" and "token" not in url.query
            else:
                authentic = "authorization" not in headers and url.query.get("token") == self.subject
            entry["auth_as_expected"] = authentic and "api-secret" not in headers
        return self.served


def states_from(hass):
    result = {}
    for name, attributes in SENSOR_ATTRIBUTES.items():
        state = hass.states.get(f"sensor.three_way_{name}")
        result[name] = None if state is None else {
            "state": state.state,
            "attributes": {key: state.attributes[key] for key in attributes if key in state.attributes},
        }
    return result


async def test_observe(hass, aioclient_mock, freezer, enable_custom_integrations):
    """Child entry point. Compare in the parent so case evidence survives failures."""
    from homeassistant import config_entries
    from homeassistant.const import CONF_API_KEY, CONF_URL
    from homeassistant.data_entry_flow import FlowResultType

    scene, mode = json.loads(Path("case.json").read_text(encoding="utf-8"))
    freezer.move_to(CLOCK)
    site = FixtureSite(scene, mode)
    aioclient_mock.clear_requests()
    aioclient_mock.get(re.compile(rf"^{re.escape(URL)}/"), side_effect=site.handle)
    result = {"phase": "config_flow", "entry_loaded": False}
    try:
        flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        if flow["type"] == FlowResultType.FORM:
            flow = await hass.config_entries.flow.async_configure(
                flow["flow_id"], {CONF_URL: URL, CONF_API_KEY: site.subject},
            )
        result["flow_type"] = str(flow["type"])
        module = import_module(f"custom_components.{DOMAIN}")
        origin = Path(module.__file__).resolve()
        expected_origin = (COMPONENT / "__init__.py").resolve()
        if origin != expected_origin:
            raise RuntimeError("Imported the wrong product stage")
        result["import_origin"] = str(origin.relative_to(Path.cwd()))
        result["implementation_sha256"] = hashlib.sha256(origin.read_bytes()).hexdigest()
        result["versions"] = {"python": sys.version.split()[0], **{
            name: metadata.version(name) for name in ("homeassistant", "pytest", "pytest-homeassistant-custom-component")
        }}
        if flow["type"] == FlowResultType.CREATE_ENTRY:
            entry = flow["result"]
            await hass.async_block_till_done()
            result["entry_loaded"] = entry.state == config_entries.ConfigEntryState.LOADED
            if result["entry_loaded"]:
                result["phase"] = "states"
                result["sensors"] = states_from(hass)
                if mode in {"candidate_legacy", "candidate_v3"} and result["sensors"]["glucose"] is not None:
                    result["phase"] = "public_history"
                    if hass.services.has_service(DOMAIN, "get_glucose_history"):
                        try:
                            result["history"] = await hass.services.async_call(
                                DOMAIN, "get_glucose_history", {
                                    "entity_id": "sensor.three_way_glucose",
                                    "start": "2026-01-08T11:59:00+00:00", "end": CLOCK,
                                }, blocking=True, return_response=True,
                            )
                        except Exception as error:
                            result["public_history_error"] = type(error).__name__
                result["phase"] = "unload"
                result["entry_unloaded"] = await hass.config_entries.async_unload(entry.entry_id)
        result["phase"] = "wire_evidence"
        result["routes"] = site.wire_evidence(aioclient_mock)
        result["phase"] = "observed"
    except Exception as error:
        result["harness_error"] = type(error).__name__
    Path("observed.json").write_text(json.dumps(result), encoding="utf-8")


def expected_sensors(scene, mode):
    # Explicit acceptance oracle for UI state. Its values are independent of
    # wire construction and of the installed client's parsers.
    newer = scene == "newer_raw" and mode in {"candidate_legacy", "candidate_v3"}
    glucose_state, delta_state = ("162", "unknown") if newer else ("153", "7")
    original_time = "2026-01-08T12:00:00+00:00"
    measured_time = "2026-01-08T12:07:00+00:00" if newer else original_time
    first_target = "2026-01-08T12:15:02+00:00"
    second_target = "2026-01-08T12:30:02+00:00"
    first = {"unit_of_measurement": "mg/dL"}
    second = {
        "unit_of_measurement": "mg/dL", "model": "AR2", "forecast": [
            {"time": first_target, "mgdl": 207}, {"time": second_target, "mgdl": 231},
        ],
    }
    if mode != "upstream_legacy":
        first.update(source_time=original_time, forecast_time=first_target)
        second.update(source_time=original_time, forecast_time=second_target)
    return {
        "glucose": {"state": glucose_state, "attributes": {
            "unit_of_measurement": "mg/dL", "direction": "FortyFiveUp", "delta": None if newer else 7,
        }},
        "glucose_change": {"state": delta_state, "attributes": {"unit_of_measurement": "mg/dL"}},
        "glucose_trend": {"state": "forty_five_up", "attributes": {}},
        "last_reading": {"state": measured_time, "attributes": {}},
        "forecast_in_15_minutes": {"state": "207", "attributes": first},
        "forecast_in_30_minutes": {"state": "231", "attributes": second},
    }


def expected_routes(mode):
    common = {"v1.status": 200, "v2.bgnow": 200, "v2.properties": 200}
    if mode == "upstream_legacy":
        return {**common, "v1.treatments": 200}
    if mode == "candidate_v3":
        return {**common, "v3.version": 200, "v2.exchange": 200, "v3.entries.search": 200,
                "v3.entries.history": 200, "v3.last_modified": 200,
                "v3.treatments": 200, "v3.devicestatus": 200}
    raw_status = 403 if mode == "candidate_legacy_denied" else 200
    return {**common, "v3.version": 404, "v1.entries": raw_status,
            "v1.treatments": 200, "v1.devicestatus": 200}


def classify(scene, mode, observed):
    if "harness_error" in observed:
        return {**observed, "status": "harness_error"}
    failures = []

    def check(field, actual, expected):
        if actual != expected:
            failures.append({"field": field, "expected": expected, "actual": actual})

    check("entry_loaded", observed.get("entry_loaded"), True)
    check("entry_unloaded", observed.get("entry_unloaded"), True)
    for key, expected in expected_sensors(scene, mode).items():
        check(f"sensors.{key}", observed.get("sensors", {}).get(key), expected)
    routes = observed.get("routes", [])
    expected_wire = [[1767873600000, 153]]
    if scene == "newer_raw":
        expected_wire.append([1767874020000, 162])
    check("routes.responses", sorted({(route["route"], route["status"]) for route in routes}),
          sorted(expected_routes(mode).items()))
    for index, route in enumerate(routes):
        check(f"routes[{index}].auth_as_expected", route["auth_as_expected"], True)
        check(f"routes[{index}].fixture_origin", route["fixture_origin"], True)
        check(f"routes[{index}].method", route["method"], "GET")
        if route["route"] == "v2.properties":
            check(f"routes[{index}].bgnow", route.get("bgnow"), [1767873600000, 153])
    if mode in {"candidate_legacy", "candidate_v3"}:
        positive_route = "v1.entries" if mode == "candidate_legacy" else "v3.entries.search"
        expected_page = list(reversed(expected_wire)) if mode == "candidate_legacy" else expected_wire
        pages = [route.get("measurements", []) for route in routes if route["route"] == positive_route]
        if expected_page not in pages:
            failures.append({"field": "routes.raw_measurements", "expected": expected_page, "actual": pages})
        expected_rows = [{"id": "111111111111111111111111", "date": 1767873600000, "sgv": 153,
                          "direction": "FortyFiveUp"}]
        if scene == "newer_raw":
            expected_rows.append({"id": "222222222222222222222222", "date": 1767874020000, "sgv": 162,
                                  "direction": "FortyFiveUp"})
        check("history", observed.get("history"), {
            "readings": expected_rows, "unit": "mg/dL", "status": "complete", "missing_ranges": [],
            "as_of": "2026-01-08T12:10:00+00:00",
        })
    return {**observed, "status": "mismatch" if failures else "pass", "failures": failures}


def stage_product(source, stage):
    staged = stage / COMPONENT
    shutil.copytree(source / COMPONENT, staged, ignore=shutil.ignore_patterns("__pycache__", "*.py[co]"))
    files = {
        path.relative_to(staged).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(staged.rglob("*")) if path.is_file()
    }
    if not {"__init__.py", "manifest.json"} <= files.keys():
        raise FileNotFoundError("Missing HA integration source")
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def run_case(scene, mode, source, interpreter):
    stage_digest = None
    try:
        with tempfile.TemporaryDirectory(prefix="ns-three-way-") as temp:
            stage = Path(temp)
            stage_digest = stage_product(source, stage)
            shutil.copyfile(__file__, stage / "test_three_way.py")
            (stage / "case.json").write_text(json.dumps([scene, mode]), encoding="utf-8")
            (stage / "conftest.py").write_text(
                'pytest_plugins = ["pytest_homeassistant_custom_component"]\n', encoding="utf-8",
            )
            (stage / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\npythonpath = .\n", encoding="utf-8")
            inherited = ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
            environment = {key: os.environ[key] for key in inherited if key in os.environ}
            finished = subprocess.run(
                [str(interpreter), "-I", "-m", "pytest", "-c", "pytest.ini", "-q", "--tb=no",
                 "--disable-socket", "--allow-unix-socket", "test_three_way.py::test_observe"],
                cwd=stage, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=False,
            )
            observation = stage / "observed.json"
            if finished.returncode != 0 or not observation.is_file():
                return {"status": "harness_error", "phase": "pytest_child", "pytest_exit": finished.returncode,
                        "stage_sha256": stage_digest}
            observed = json.loads(observation.read_text(encoding="utf-8"))
            return {"stage_sha256": stage_digest, **classify(scene, mode, observed)}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {"status": "harness_error", "phase": "stage_or_child", "harness_error": type(error).__name__,
                "stage_sha256": stage_digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("upstream", "candidate", "ha-python", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--case", choices=CASES, action="append", help="Select a row. Default is all eight rows.")
    args = parser.parse_args()
    interpreter = args.ha_python.absolute()  # Preserve a virtualenv's Python symlink.
    observations = {}
    for case in dict.fromkeys(args.case or CASES):
        scene, mode = case.split("/")
        source = args.upstream if mode == "upstream_legacy" else args.candidate
        observations[case] = run_case(scene, mode, source.resolve(), interpreter)
    statuses = {observation["status"] for observation in observations.values()}
    status = next(value for value in ("harness_error", "mismatch", "pass") if value in statuses)
    report = {"schema_version": 1, "status": status, "observed_at": CLOCK, "cases": observations}
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"Unable to write three-way receipt ({type(error).__name__})", file=sys.stderr)
        return 2
    print(f"Three-way HA fixture: {status} ({len(observations)} observations)")
    return {"pass": 0, "mismatch": 1, "harness_error": 2}[status]


if __name__ == "__main__":
    sys.exit(main())
