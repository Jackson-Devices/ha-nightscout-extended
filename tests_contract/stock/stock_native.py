"""Direct stock Nightscout read oracle. This module imports no HA product code."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlencode


HARNESS = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(HARNESS))
from server_v3 import AUTH, await_read, native_subject, request  # noqa: E402


VERSION = "15.0.6"
SOURCE = "9cd304f78a5c12401c9711cbd56d2a12eaca0632"
IMAGE = "docker.io/nightscout/cgm-remote-monitor:15.0.6@sha256:868f024f0aaf746aa9c6f639b4add7a78b07bcf4382f9955c5af61d413cad989"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
THRESHOLDS = {"bgLow": 55, "bgTargetBottom": 80, "bgTargetTop": 180, "bgHigh": 260}
MAINTENANCE = {
    "Sensor Change": 20,
    "Insulin Change": 40,
    "Site Change": 60,
    "Pump Battery Change": 96,
}
COLLECTIONS = ("entries", "treatments", "devicestatus", "profile", "food", "settings")
PROPERTIES = "/api/v2/properties/bgnow,delta,direction,iob,cob,basal,ar2"
UPLOADER = "stock-parity-pump"
SGV_DEVICE = "stock-parity-cgm"
QUANTITIES = ("carbs", "insulin", "duration", "rate", "absolute")
V1_COERCION = "lib/api/treatments/index.js:serveTreatments"


class StockError(Exception):
    """Fixed, credential-free gate name."""


def check(condition, phase):
    if not condition:
        raise StockError(phase)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc(value):
    if type(value) is int:
        value = EPOCH + timedelta(milliseconds=value)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ms(value):
    return (value - EPOCH) // timedelta(milliseconds=1)


def timestamp(value, phase):
    check(type(value) is str, phase)
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    check(when.tzinfo is not None, phase)
    return utc(when.astimezone(UTC))


def numeric(value, phase):
    check(type(value) in (int, float) and math.isfinite(value), phase)
    return value


def integer(value, phase):
    check(type(value) is int, phase)
    return value


def mapping(value, phase):
    check(type(value) is dict, phase)
    return value


def documents(value, phase):
    check(type(value) is list and all(type(row) is dict for row in value), phase)
    return value


def oid(slot):
    return {"$oid": f"{0x650000000000000000000000 + slot:024x}"}


def seed(anchor):
    """Create one fixture with distinct measurement, event and contact dates."""
    entries = []
    for slot, minutes, value, direction in (
        (1, 5 * 24 * 60, 112, "Flat"),
        (2, 11, 157, "Flat"),
        (3, 6, 168, "SingleUp"),
        (4, 1, 180, "FortyFiveUp"),
    ):
        when = anchor - timedelta(minutes=minutes)
        entries.append({
            "_id": oid(slot), "type": "sgv", "date": ms(when), "dateString": utc(when),
            "sgv": value, "direction": direction, "device": SGV_DEVICE,
        })

    def treatment(slot, when, kind, **quantities):
        return {"_id": oid(slot), "eventType": kind, "created_at": utc(when),
                "date": ms(when), **quantities}

    treatments = [
        treatment(11 + index, anchor - timedelta(hours=hours), kind)
        for index, (kind, hours) in enumerate(MAINTENANCE.items())
    ]
    treatments.append(treatment(15, anchor - timedelta(hours=100), "Insulin Change"))
    treatments.extend((
        treatment(16, anchor - timedelta(minutes=90), "Temp Basal", duration=30, rate=1.25, absolute=1.25),
        treatment(17, anchor - timedelta(minutes=55), "Correction Bolus", carbs=0, insulin=1.25),
        treatment(18, anchor - timedelta(minutes=48), "Snack Bolus", carbs=14.5),
    ))

    device = []
    for slot, upload_delay, contact_delay, amount in ((21, 3, 4, 17.5), (22, 1, 2, 0)):
        upload = anchor - timedelta(minutes=upload_delay, seconds=15)
        contact = anchor - timedelta(minutes=contact_delay)
        device.append({
            "_id": oid(slot), "device": UPLOADER, "created_at": utc(upload), "date": ms(upload),
            "pump": {"reservoir": amount, "status": {"timestamp": utc(contact)}},
        })
    device[-1]["loop"] = {
        "iob": {"iob": 1.375, "timestamp": device[-1]["created_at"]},
        "cob": {"cob": 7.5, "timestamp": device[-1]["created_at"]},
    }
    profile_at = anchor - timedelta(days=7)
    profile = [{
        "_id": oid(31), "date": ms(profile_at), "created_at": utc(profile_at),
        "startDate": utc(profile_at), "defaultProfile": "Synthetic", "units": "mg/dl",
        "store": {"Synthetic": {
            "dia": 3, "carbs_hr": 20, "units": "mg/dl", "timezone": "Etc/UTC",
            **{name: [{"time": "00:00", "value": value, "timeAsSeconds": 0}]
               for name, value in {
                   "basal": 0.85, "sens": 48, "carbratio": 10, "target_low": 90, "target_high": 110,
               }.items()},
        }},
    }]
    return {"entries": entries, "treatments": treatments, "devicestatus": device, "profile": profile}


def query(path, params):
    return f"{path}?{urlencode(params)}" if params else path


class ObservedGets:
    """Validate successful stock reads before the fixture oracle looks at values."""

    def __init__(self, port, gate, sources):
        self.port = port
        self.gate = gate
        self.sources = sources
        self.routes = {}

    def fetch(self, path, headers, params=None):
        status, body = request(self.port, query(path, params), headers=headers)
        if status == 200:
            self.observe(path, body)
        return status, body

    def get(self, path, headers, params=None):
        status, body = self.fetch(path, headers, params)
        check(status == 200, f"native_get:{path}:http_{status}")
        return body

    def observe(self, path, body):
        # The v1/v2 status route can echo exchanged authorization when called
        # with a query token. This runner uses only a header bearer or no auth.
        if path in ("/api/v1/status.json", "/api/v2/status.json"):
            check(not isinstance(body, dict) or body.get("authorized") is None,
                  "stock_status_unexpected_authorization_payload")
        api = path.split("/")[2]
        route = path
        if path.startswith("/api/v3/entries/history/"):
            route = "/api/v3/entries/history/{lastModified}"
        entry = self.routes.setdefault((api, route), {"observations": 0, "clean_observations": 0,
                                                       "diagnostics": {}})
        entry["observations"] += 1
        try:
            diagnostics = self.gate.validate(api, path, body, method="GET", status=200)
            for diagnostic in diagnostics:
                check(diagnostic["classification"] in ("schema-mismatch", "source-only", "unverified"),
                      "unknown_swagger_diagnostic_classification")
                key = canonical(diagnostic)
                if key not in entry["diagnostics"]:
                    entry["diagnostics"][key] = {"diagnostic": diagnostic, "occurrences": 0}
                entry["diagnostics"][key]["occurrences"] += 1
        except Exception as exc:
            entry["failure"] = type(exc).__name__
            raise StockError("stock_swagger_validation_error") from None
        if not diagnostics:
            entry["clean_observations"] += 1

    def receipt(self):
        classes = {"schema-mismatch": 0, "source-only": 0, "unverified": 0}
        output = []
        faults = False
        for (api, route), entry in sorted(self.routes.items()):
            findings = [entry["diagnostics"][key] for key in sorted(entry["diagnostics"])]
            route_classes = {finding["diagnostic"]["classification"] for finding in findings}
            for finding in findings:
                classes[finding["diagnostic"]["classification"]] += finding["occurrences"]
            view = {key: value for key, value in entry.items() if key != "diagnostics"}
            faults |= "failure" in entry
            if "failure" in entry or "unverified" in route_classes:
                route_status = "harness_error"
            elif "schema-mismatch" in route_classes:
                route_status = "mismatch"
            else:
                route_status = "source-only" if "source-only" in route_classes else "pass"
            output.append({"api": api, "method": "GET", "route": route, "http": 200,
                           "result": route_status, **view, "diagnostics": findings})
        if faults or classes["unverified"] or not output:
            status = "harness_error"
        elif classes["schema-mismatch"]:
            status = "mismatch"
        else:
            status = "pass"
        return {"status": status, "sources": self.sources, "diagnostic_counts": classes, "routes": output}


def v3_result(body, path):
    body = mapping(body, path)
    check(type(body.get("status")) is int and body["status"] == 200, f"v3_envelope:{path}")
    return body.get("result")


def ready(wire, reader, latest):
    def ready_for_seed(value):
        if not isinstance(value, dict):
            return False
        now = value.get("bgnow", {})
        return isinstance(now, dict) and (now.get("last"), now.get("mills")) == (latest["sgv"], latest["date"])

    return await_read(lambda: wire.fetch(PROPERTIES, reader), ready_for_seed, "stock seeded bgnow")


def permission_matrix(port, reader, wire):
    checks = {}
    for domain in ("status", *COLLECTIONS):
        for action in ("read", "create", "update", "delete", "admin"):
            right = f"api:{domain}:{action}"
            path = f"{AUTH}/debug/check/{quote(right, safe=':')}"
            outcome = {}
            for tag, header in (("subject", reader), ("anonymous", {})):
                status, body = request(port, path, headers=header)
                if action == "read":
                    check(status == 200 and body == {"check": True}, f"read_right:{right}:{tag}")
                else:
                    check(status in (401, 403), f"excess_right:{right}:{tag}")
                outcome[tag] = status
            check(outcome["subject"] == outcome["anonymous"], f"permission_inequality:{right}")
            checks[right] = outcome

    version = mapping(v3_result(wire.get("/api/v3/version", {}), "version"), "version")
    status = mapping(v3_result(wire.get("/api/v3/status", reader), "status"), "status")
    for result in (version, status):
        check(result.get("version") == VERSION and str(result.get("apiVersion", "")).split(".")[0] == "3",
              "stock_v3_version")
    granted = status.get("apiPermissions")
    check(granted == {name: "r" for name in COLLECTIONS}, "stock_v3_effective_permissions")
    # V3 settings SEARCH requires settings:admin even when its status lists settings:r.
    settings_status, _ = request(port, "/api/v3/settings", headers=reader)
    anonymous_v3, _ = request(port, "/api/v3/entries?limit=1")
    check(settings_status == 403 and anonymous_v3 == 401, "stock_v3_security_boundary")
    return {
        "routes": {"debug": f"{AUTH}/debug/check/<permission>", "status": "/api/v3/status",
                   "version": "/api/v3/version"},
        "checks": checks, "v3_effective": granted, "v3_anonymous_entries_http": anonymous_v3,
        "v3_settings_collection_http": settings_status, "nightscout": VERSION, "api": version["apiVersion"],
    }


def row_id(row):
    ident = row.get("identifier", row.get("_id"))
    check(type(ident) is str and re.fullmatch(
        r"(?:[0-9a-f]{24}|[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})", ident
    ), "native_row_identity")
    return ident


def read_entry(row):
    check(row.get("type") == "sgv" and row.get("direction") in ("Flat", "SingleUp", "FortyFiveUp"),
          "native_sgv_type_or_direction")
    return {"id": row_id(row), "date": integer(row.get("date"), "sgv_date"),
            "sgv": numeric(row.get("sgv"), "sgv_value"), "direction": row["direction"]}


def read_treatment(row):
    kind = row.get("eventType")
    check(kind in (*MAINTENANCE, "Temp Basal", "Correction Bolus", "Snack Bolus"), "native_treatment_event")
    at = timestamp(row.get("created_at"), "treatment_created_at")
    when = integer(row.get("date"), "treatment_date")
    check(utc(when) == at, "treatment_timestamp_disagrees")
    measures = {field: numeric(row[field], f"treatment_{field}") if row.get(field) is not None else None
                for field in QUANTITIES}
    return {"id": row_id(row), "at": at, "date": when, "event_type": kind, **measures}


def read_device(row):
    check(row.get("device") == UPLOADER, "native_uploader")
    pump = mapping(row.get("pump"), "native_pump_object")
    status = mapping(pump.get("status"), "native_pump_status_object")
    at = timestamp(row.get("created_at"), "device_created_at")
    when = integer(row.get("date"), "device_date")
    check(utc(when) == at, "device_timestamp_disagrees")
    loop = mapping(row.get("loop", {}), "native_loop")
    reported = {}
    for field in ("iob", "cob"):
        if field in loop:
            current = mapping(loop[field], f"native_loop_{field}")
            reported[field] = {
                "value": numeric(current.get(field), f"native_loop_{field}_value"),
                "at": timestamp(current.get("timestamp"), f"native_loop_{field}_timestamp"),
            }
    return {"id": row_id(row), "at": at, "date": when, "uploader": UPLOADER,
            "contact_at": timestamp(status.get("timestamp"), "native_reservoir_contact"),
            "reservoir": numeric(pump.get("reservoir"), "native_reservoir"), "loop": reported}


def read_profile(row):
    check(row.get("defaultProfile") == "Synthetic", "native_profile_name")
    store = mapping(mapping(row.get("store"), "profile_store").get("Synthetic"), "profile_default_store")
    rates = {}
    for field in ("basal", "sens", "carbratio"):
        values = documents(store.get(field), f"profile_{field}_schedule")
        check(len(values) == 1 and values[0].get("time") == "00:00", f"profile_{field}_schedule")
        rates[field] = numeric(values[0].get("value"), f"profile_{field}_value")
    return {"id": row_id(row), "starts": timestamp(row.get("startDate"), "profile_start_date"), **rates}


def fixture_data(collection, normalize):
    return [normalize({**row, "_id": row["_id"]["$oid"]}) for row in collection]


def presence(row, field, *, numeric_type=False):
    if field not in row:
        return "absent"
    if row[field] is None:
        return "null"
    return "number" if numeric_type else type(row[field]).__name__


def document_proof(rows, values, fields, envelope):
    shape = [{"id": row_id(row), "fields": {field: presence(row, field, numeric_type=field in QUANTITIES)
                                            for field in fields}} for row in rows]
    return {"envelope": envelope, "rows": values, "wire_shape": shape,
            "readback_sha256": digest({"values": values, "shape": shape})}


def collection_reads(wire, reader, fixture, anchor):
    start = anchor - timedelta(days=6)
    end = utc(anchor)
    scope = {
        "entries": (read_entry, ("_id", "identifier", "type", "date", "sgv", "direction")),
        "treatments": (read_treatment, ("_id", "identifier", "date", "created_at", "eventType", *QUANTITIES)),
        "devicestatus": (read_device, ("_id", "identifier", "date", "created_at", "device", "pump", "loop")),
        "profile": (read_profile, ("_id", "identifier", "created_at", "startDate", "defaultProfile", "store")),
    }
    pending = []
    for api in (1, 2, 3):
        for name, (normalize, fields) in scope.items():
            if api == 3:
                endpoint = f"/api/v3/{name}"
                parameters = {"limit": 30}
                if name == "entries":
                    parameters.update({"type": "sgv", "date$gte": ms(start), "date$lt": ms(anchor),
                                       "sort$desc": "date"})
                elif name != "profile":
                    parameters.update({"created_at$gte": utc(start), "created_at$lt": end,
                                       "sort$desc": "created_at"})
            else:
                endpoint = f"/api/v{api}/" + {
                    "entries": "entries/sgv.json", "treatments": "treatments.json",
                    "devicestatus": "devicestatus/", "profile": "profile/",
                }[name]
                parameters = {"count": 30}
                if name == "entries":
                    parameters.update({"find[date][$gte]": ms(start), "find[date][$lt]": ms(anchor)})
                elif name != "profile":
                    parameters.update({"find[created_at][$gte]": utc(start), "find[created_at][$lt]": end})
            payload = wire.get(endpoint, reader, parameters)
            pending.append((api, name, normalize, fields, endpoint, parameters, payload))

    # Every searched collection reaches the official schema gate even if an
    # incompatible raw type subsequently prevents the independent seed oracle.
    reads = {"1": {}, "2": {}, "3": {}}
    shape_differences = []
    for api, name, normalize, fields, endpoint, parameters, payload in pending:
        envelope = "object:status,result" if api == 3 else "array"
        rows = documents(v3_result(payload, endpoint) if api == 3 else payload, endpoint)
        values = [normalize(row) for row in rows]
        expected = fixture_data(fixture[name], normalize)
        if name == "entries":
            check(values == list(reversed(expected)), f"stock_ordered_sgv_disagreement:v{api}")
        else:
            check(sorted(values, key=lambda row: row["id"]) == expected, f"stock_seed_disagreement:{endpoint}")
        if name == "treatments":
            expected_by_id = {row["_id"]["$oid"]: row for row in fixture[name]}
            for row in rows:
                ident = row_id(row)
                for field in QUANTITIES:
                    expected_shape = (
                        "number" if field in expected_by_id[ident]
                        else "null" if api != 3 and field in ("carbs", "insulin")
                        else "absent"
                    )
                    actual_shape = presence(row, field, numeric_type=True)
                    if actual_shape != expected_shape:
                        shape_differences.append({"route": endpoint, "id": ident, "field": field,
                                                  "expected": expected_shape, "actual": actual_shape})
        proof = document_proof(rows, values, fields, envelope)
        reads[str(api)][name] = {"route": endpoint, "query": parameters, "http": 200, **proof}
    return reads, shape_differences


def status_read(wire, path, headers, latest):
    payload = mapping(wire.get(path, headers), path)
    check(payload.get("version") == VERSION and payload.get("status") == "ok"
          and payload.get("apiEnabled") is True, "stock_legacy_version_and_status")
    context = mapping(payload.get("settings"), "stock_settings")
    thresholds = mapping(context.get("thresholds"), "stock_thresholds")
    thresholds = {name: numeric(thresholds.get(name), name) for name in THRESHOLDS}
    check(thresholds == THRESHOLDS, "stock_threshold_values")
    unit = context.get("units")
    check(type(unit) is str and unit.lower() == "mg/dl", "stock_glucose_unit")
    warning = {}
    for kind, enabled, minutes in (("warn", "alarmTimeagoWarn", "alarmTimeagoWarnMins"),
                                   ("urgent", "alarmTimeagoUrgent", "alarmTimeagoUrgentMins")):
        setting = context.get(enabled)
        check(type(setting) is bool, "stock_timeago_enabled_kind")
        warning[kind] = {"enabled": setting, "minutes": numeric(context.get(minutes), minutes)}
    clock = integer(payload.get("serverTimeEpoch"), "stock_clock")
    check(0 < clock - latest["date"] < 15 * 60_000, "stock_seed_clock_freshness")
    values = {"version": VERSION, "glucose_unit": "mg/dL", "thresholds": thresholds,
              "status": "ok", "api_enabled": True, "timeago": warning}
    return {"route": path, "http": 200, "envelope": "object", "server_clock_ms": clock,
            "values": values, "readback_sha256": digest(values)}


def properties_read(wire, headers, latest, previous, device):
    props = mapping(wire.get(PROPERTIES, headers), "v2_properties_object")
    bgnow = mapping(props.get("bgnow"), "stock_bgnow")
    current = {"last": numeric(bgnow.get("last"), "stock_current_sgv"),
               "mills": integer(bgnow.get("mills"), "stock_current_time"),
               "mean": numeric(bgnow.get("mean"), "stock_current_mean")}
    check((current["last"], current["mills"], current["mean"]) ==
          (latest["sgv"], latest["date"], latest["sgv"]), "stock_bgnow_seed_disagreement")
    delta = mapping(props.get("delta"), "stock_delta")
    change = {name: numeric(delta.get(name), f"delta_{name}")
              for name in ("mgdl", "absolute", "elapsedMins", "mean5MinsAgo")}
    change_times = mapping(delta.get("times"), "stock_delta_times")
    check(change_times == {"recent": latest["date"], "previous": previous["date"]}
          and change == {"mgdl": latest["sgv"] - previous["sgv"], "absolute": latest["sgv"] - previous["sgv"],
                         "elapsedMins": 5, "mean5MinsAgo": previous["sgv"]}, "stock_delta_seed_disagreement")
    direction = mapping(props.get("direction"), "stock_direction").get("value")
    check(direction == latest["direction"], "stock_direction_seed_disagreement")

    optional = {}
    for field in ("iob", "cob"):
        obj = mapping(props.get(field), f"stock_{field}_object")
        source = obj.get("source")
        value = numeric(obj.get(field), f"stock_{field}_value")
        when = integer(obj.get("mills"), f"stock_{field}_when")
        expected = device["loop"][field]
        check(source == "Loop" and value == expected["value"] and utc(when) == expected["at"],
              f"stock_{field}_seed_disagreement")
        optional[field] = {"value": value, "source": source, "mills": when}
    basal = mapping(mapping(props.get("basal"), "stock_basal").get("current"), "stock_basal_current")
    basal_rate = numeric(basal.get("totalbasal"), "stock_basal_value")
    check(basal_rate == 0.85, "stock_basal_seed_disagreement")

    ar2 = mapping(props.get("ar2"), "stock_ar2_object")
    forecast = mapping(ar2.get("forecast"), "stock_ar2_forecast")
    raw = documents(forecast.get("predicted"), "stock_ar2_point_array")
    points = [[integer(p.get("mills"), "stock_ar2_point_time"),
               numeric(p.get("mgdl"), "stock_ar2_point_value")] for p in raw]
    # Both keys and six points are native. ar2.pushPoint adds 2 seconds to each
    # 5-minute forecast step from its own measurement source.
    check([p[0] for p in points] == [current["mills"] + step * 300_000 + 2000 for step in range(1, 7)]
          and all(36 <= p[1] <= 400 for p in points), "stock_ar2_point_fence")
    values = {"bgnow": current, "delta": change, "direction": direction,
              "iob": optional["iob"], "cob": optional["cob"], "basal": basal_rate, "ar2": points}
    return {
        "route": PROPERTIES, "http": 200, "envelope": "object",
        "wire_shape": {key: presence(props, key) for key in
                       ("bgnow", "delta", "direction", "iob", "cob", "basal", "ar2")},
        "ar2_shape": {"field": "ar2.forecast.predicted", "shape": "list[{mills: integer, mgdl: number}]"},
        "values": values, "readback_sha256": digest(values),
    }


def fence(wire, reader):
    path = "/api/v3/lastModified"
    value = mapping(v3_result(wire.get(path, reader), path), "lastmodified_object")
    times = mapping(value.get("collections"), "lastmodified_collections")
    return {name: integer(times.get(name), f"lastmodified_{name}")
            for name in ("entries", "treatments", "devicestatus", "profile")}


def snapshot(wire, reader, fixture, anchor):
    reads, shape_differences = collection_reads(wire, reader, fixture, anchor)
    recent = reads["3"]["entries"]["rows"]
    device = max(reads["3"]["devicestatus"]["rows"], key=lambda row: row["contact_at"])
    # Verify that anonymous legacy sees the same native data, in addition to the
    # individual permission checks, before giving either legacy profile no token.
    for collection in reads["1"].values():
        rows = wire.get(collection["route"], {}, collection["query"])
        authenticated_rows = wire.get(collection["route"], reader, collection["query"])
        check(rows == authenticated_rows, "anonymous_legacy_collection_differs")
        collection["anonymous_matches"] = True
    status = [status_read(wire, f"/api/v{version}/status.json", reader, recent[0]) for version in (1, 2)]
    anon_status = status_read(wire, "/api/v1/status.json", {}, recent[0])
    check(all(item["values"] == anon_status["values"] for item in status), "legacy_native_status_disagreement")
    status[0]["anonymous_matches"] = True
    props = properties_read(wire, reader, recent[0], recent[1], device)
    anon_props = properties_read(wire, {}, recent[0], recent[1], device)
    check(anon_props == props, "anonymous_legacy_properties_differ")
    props["anonymous_matches"] = True
    absent = {}
    for path in ("/api/v1/treatments.json", "/api/v2/treatments.json", "/api/v3/treatments"):
        key = "eventType" if path.startswith("/api/v3/") else "find[eventType]"
        response = wire.get(path, reader, {key: "Synthetic Missing Maintenance"})
        rows = v3_result(response, path) if path.startswith("/api/v3/") else response
        check(rows == [], "stock_absent_maintenance_control")
        absent[path] = {"event_type": "Synthetic Missing Maintenance", "rows": [], "http": 200}
    return {"collections": reads, "status": status, "properties": props,
            "treatment_wire_source": {"v1_get": V1_COERCION, "v2_alias": "lib/api2/index.js",
                                      "fixture_shape_differences": shape_differences},
            "storage_fence": fence(wire, reader), "absence_controls": absent}


def stable_view(state):
    """Exclude only status wall-clock diagnostics from the equality fence."""
    return {**state, "status": [{key: value for key, value in row.items() if key != "server_clock_ms"}
                                for row in state["status"]]}


def expected_for_ha(state, anchor):
    raw = state["collections"]["3"]
    now = state["properties"]["values"]
    newest = raw["entries"]["rows"][0]
    devices = raw["devicestatus"]["rows"]
    latest = max(devices, key=lambda row: row["contact_at"])
    check(latest["reservoir"] == 0 and len({row["reservoir"] for row in devices}) == 2,
          "stock_zero_reservoir_control")
    treatments = raw["treatments"]["rows"]
    maintenance = {kind: max(row["at"] for row in treatments if row["event_type"] == kind)
                   for kind in MAINTENANCE}
    intake = [{k: v for k, v in row.items() if k not in ("id", "date", "absolute")}
              for row in sorted(treatments, key=lambda row: row["date"])
              if row["event_type"] not in MAINTENANCE]
    history_start = anchor - timedelta(minutes=15)
    history = sorted((row for row in raw["entries"]["rows"] if ms(history_start) <= row["date"] < ms(anchor)),
                     key=lambda row: (row["date"], row["id"]))
    check(len(intake) == 3 and len(history) == 3, "stock_intake_history_control")
    return {
        "sgv": newest["sgv"], "sgv_at": utc(newest["date"]), "delta": now["delta"]["mgdl"],
        "direction": now["direction"], "forecast_source_at": utc(now["bgnow"]["mills"]), "ar2_points": now["ar2"],
        "iob": now["iob"]["value"], "iob_source": now["iob"]["source"],
        "cob": now["cob"]["value"], "cob_source": now["cob"]["source"], "basal": now["basal"],
        "reservoir": latest["reservoir"], "uploader": latest["uploader"], "device_at": latest["contact_at"],
        "maintenance": maintenance, "intake": intake, "glucose_unit": "mg/dL",
        "thresholds": state["status"][0]["values"]["thresholds"],
        "calendar_window": [utc(anchor - timedelta(minutes=70)), utc(anchor - timedelta(minutes=40))],
        "history_window": [utc(history_start), utc(anchor)], "history": history,
    }


def late_history(port, wire, reader, admin, state, anchor):
    """Exercise the real read-only V3 history after the profile comparison."""
    cursor = state["storage_fence"]["entries"]
    route = "/api/v3/entries/history/<storage-ms>"

    def read(since):
        path = f"/api/v3/entries/history/{since}"
        payload = wire.get(path, reader, {"limit": 30})
        return documents(v3_result(payload, route), route)

    before = read(cursor)
    check(before == [], "stock_history_no_change")
    _, writer, writer_id, _ = native_subject(port, admin, name="stock-history-writer", role="admin")
    late_date = ms(anchor - timedelta(minutes=8))
    doc = {"type": "sgv", "date": late_date, "sgv": 164, "direction": "Flat", "device": SGV_DEVICE,
           "app": "stock-parity-history"}
    try:
        status, result = request(port, "/api/v3/entries", method="POST", headers=writer, body=doc)
        created = mapping(result, "stock_late_insert_envelope")
        check(status == 201 and created.get("status") == 201, "stock_late_create")
        ident = row_id(created)
        modified = integer(created.get("lastModified"), "stock_late_insert_modified_clock")
        expected = {"id": ident, "date": late_date, "sgv": 164, "direction": "Flat"}
        changed = read(cursor)
        check(len(changed) == 1 and read_entry(changed[0]) == expected
              and integer(changed[0].get("srvModified"), "stock_history_modified_clock") == modified
              and late_date < cursor < modified, "stock_late_history_disagreement")
        check(read(modified) == [], "stock_history_advanced_empty")
        newest_route = "/api/v3/entries"
        payload = wire.get(newest_route, reader, {"type": "sgv", "sort$desc": "date", "limit": 5})
        actual = [read_entry(row) for row in documents(v3_result(payload, newest_route), newest_route)]
        baseline = state["collections"]["3"]["entries"]["rows"]
        check(actual == sorted([*baseline, expected], key=lambda row: row["date"], reverse=True),
              "stock_late_measurement_order")
        return {"route": route, "create_route": newest_route, "before": [], "before_cursor": cursor,
                "after": expected, "after_cursor": modified, "advanced_empty": True,
                "newest_unchanged": True, "create_http": status, "history_http": 200}
    finally:
        status, _ = request(port, f"{AUTH}/subjects/{writer_id}", method="DELETE", headers={"api-secret": admin})
        check(status == 200, "stock_late_writer_cleanup")
