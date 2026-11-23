"""Pair transport modes of one real product against the version suite's stock seed."""

from __future__ import annotations

import ast
from datetime import timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re

import pytest
import subprocess
import sys

from homeassistant.const import UnitOfBloodGlucoseConcentration
from homeassistant.util.unit_conversion import BloodGlucoseConcentrationConverter

from .candidate_probe import DOMAIN, V3_PROTECTED
from .version_stock import DEBUG_PERMISSION, EPOCH, READABLE, instant, read_subject, request, require

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = Path("custom_components") / DOMAIN
COMMON = {"v1.status", "v2.bgnow", "v2.properties"}
LEGACY = {"v1.entries", "v1.treatments", "v1.devicestatus"}
NEGOTIATION = {"v3.version", "v2.exchange"}
THRESHOLDS = {
    "urgent_low": "bgLow", "low": "bgTargetBottom", "high": "bgTargetTop", "urgent_high": "bgHigh",
}
AGES = {
    "insulin_age": {"Insulin Change"}, "cannula_age": {"Site Change"},
    "sensor_age": {"Sensor Start", "Sensor Change"}, "pump_battery_age": {"Pump Battery Change"},
}
MGDL = UnitOfBloodGlucoseConcentration.MILLIGRAMS_PER_DECILITER
MMOL = UnitOfBloodGlucoseConcentration.MILLIMOLE_PER_LITER
ACTOR = "version-parity"


def _git(root, *args):
    try:
        completed = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError(f"candidate Git provenance unavailable ({type(exc).__name__})") from None
    require(completed.returncode == 0, "candidate checkout Git provenance unavailable")
    return completed.stdout.strip()


def _supports_v3(root):
    api = root / COMPONENT / "api.py"
    if not api.is_file():
        return False
    tree = ast.parse(api.read_text(encoding="utf-8"))
    client = next((node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == "NightscoutClient"), None)
    return client is not None and {"_select_v3", "_protected_v3"} <= {
        node.name for node in client.body if isinstance(node, ast.AsyncFunctionDef)
    }


def _checkout():
    """The product under test is the checked-out code. It is never swapped for another branch's code."""
    if not _supports_v3(ROOT):
        pytest.skip("not applicable: the product under test has no v3 client")
    root = ROOT
    require(Path(_git(root, "rev-parse", "--show-toplevel")).resolve() == root.resolve(),
            "candidate must be its own Git checkout")
    sha = _git(root, "rev-parse", "HEAD")
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "candidate checkout HEAD is invalid")
    require(not _git(root, "status", "--porcelain", "--untracked-files=all", "--", COMPONENT.as_posix()),
            "candidate product source differs from its reported Git commit")
    return root, sha


def _verify_header_scope(reader):
    headers = {"api-secret": hashlib.sha1(reader.access.encode()).hexdigest()}
    for domain in ("status", *READABLE):
        for action in ("read", "create", "update", "delete", "admin"):
            status, _, body = request(f"{DEBUG_PERMISSION}/api:{domain}:{action}", headers=headers)
            expected = 200 if action == "read" else 401
            require(status == expected,
                    f"legacy subject header scope for {domain}/{action}: HTTP {status}, expected {expected}")
            if action == "read":
                require(body == {"check": True}, f"legacy subject header lacks read grant on {domain}")


def _time(milliseconds):
    return (EPOCH + timedelta(milliseconds=milliseconds)).isoformat()


def _calendar_fields(row):
    parts = [f"{row[field]:g} {label}" for field, label in (("carbs", "g carbs"), ("insulin", "U insulin"))
             if type(row.get(field)) in (int, float) and row[field]]
    start = instant(row["created_at"])
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=row.get("duration") or 5)).isoformat(),
        "summary": f"{row['eventType']}: {', '.join(parts)}" if parts else row["notes"],
        "description": f"{row['notes']}\nEntered by {row['enteredBy']}",
    }


def _treatment_fields(identity, row):
    return {
        "id": identity, "time": instant(row["created_at"]).isoformat(), "event_type": row["eventType"],
        "notes": row["notes"], "entered_by": row["enteredBy"],
        **{field: row.get(field) if type(row.get(field)) in (int, float) else None
           for field in ("carbs", "insulin", "duration")},
    }


def _expected(api, anchor, wire_fixture, calendar_range, history_range):
    _, statuses, collections, _, props = wire_fixture
    raw = collections[api, "entries"]
    current = max(raw.values(), key=lambda row: row["date"])
    current_time = _time(current["date"])
    settings = statuses[1]["settings"]
    unit = MMOL if settings["units"].lower().startswith("mmol") else MGDL
    convert = BloodGlucoseConcentrationConverter.converter_factory(MGDL, unit)
    delta = props["delta"]["mgdl"]
    direction = current["direction"]
    treatments = collections[api, "treatments"]

    def state(value, unit=None, **attrs):
        return {"value": value, "unit": unit, **attrs}

    states = {
        "glucose": state(convert(current["sgv"]), unit, direction=direction, delta=delta,
                         thresholds_mgdl={role: settings["thresholds"][key] for role, key in THRESHOLDS.items()}),
        "glucose_delta": state(convert(delta), unit),
        "glucose_trend": state(re.sub(r"(?<=[a-z])(?=[A-Z])", "_", direction).lower()),
        "last_reading": state(instant(current_time).replace(microsecond=0).isoformat()),
        "iob": state(props["iob"]["iob"], "U", source=props["iob"]["source"]),
        "cob": state(props["cob"]["cob"], "g", source=props["cob"]["source"]),
        "basal": state(props["basal"]["current"]["totalbasal"], "U/h"),
    }
    for role, field, unit_name in (("last_carbs", "carbs", "g"), ("last_insulin", "insulin", "U")):
        row = max((row for row in treatments.values() if type(row.get(field)) in (int, float) and row[field]
                   and instant(row["created_at"]) <= anchor), key=lambda row: instant(row["created_at"]))
        states[role] = state(row[field], unit_name, time=instant(row["created_at"]).isoformat(),
                             event_type=row["eventType"], notes=row["notes"])

    points = props["ar2"]["forecast"]["predicted"]
    forecast_anchor = props["bgnow"]["mills"]
    for horizon in (15, 30):
        point = next(point for point in points if point["mills"] == forecast_anchor + horizon * 60_000 + 2_000)
        attrs = {"source_time": _time(forecast_anchor), "forecast_time": _time(point["mills"])}
        if horizon == 30:
            attrs["model"] = "AR2"
            attrs["forecast"] = [{"time": _time(point["mills"]), "mgdl": round(point["mgdl"])} for point in points]
        states[f"forecast_{horizon}"] = state(convert(point["mgdl"]), unit, **attrs)

    devices = collections[api, "devicestatus"].values()
    device = max(devices, key=lambda row: instant(row["pump"]["status"]["timestamp"]))
    states["insulin_remaining"] = state(
        device["pump"]["reservoir"], "U", source_time=instant(device["pump"]["status"]["timestamp"]).isoformat(),
        uploader=device["device"],
    )
    for role, events in AGES.items():
        when, event = max((instant(row["created_at"]), row["eventType"]) for row in treatments.values()
                          if row["eventType"] in events and instant(row["created_at"]) <= anchor)
        states[role] = state((anchor - when) / timedelta(hours=1), "h", source_time=when.isoformat(), event_type=event)

    start, end = calendar_range
    calendar = []
    for row in treatments.values():
        event = _calendar_fields(row)
        if instant(event["start"]) < end and instant(event["end"]) > start:
            calendar.append(event)
    start, end = history_range
    return {
        "raw_current": {"sgv": current["sgv"], "time": current_time},
        "states": states,
        "treatments": [_treatment_fields(identity, row) for identity, row in treatments.items()
                       if anchor - timedelta(hours=48) <= instant(row["created_at"]) < anchor + timedelta(days=62)],
        "calendar": calendar,
        "history": {
            "unit": MGDL, "as_of": anchor.isoformat(), "status": "complete", "missing_ranges": [],
            "readings": [
                {"id": identity, "date": row["date"], "sgv": row["sgv"], "direction": row["direction"]}
                for identity, row in raw.items() if start <= instant(_time(row["date"])) < end
            ],
        },
    }


def _allowed_text(expected):
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)

    return sorted(set(strings(expected)) | {"unknown", "unavailable", "partial"})


def _child(directory, root, mode, subject, anchor, expected, calendar_range, history_range, treatment_ids):
    directory.mkdir()
    payload = {
        "mode": mode, "product_root": str(root), "subject": subject, "clock": anchor.isoformat(),
        "calendar_range": [time.isoformat() for time in calendar_range],
        "history_range": [time.isoformat() for time in history_range],
        "calendar_times": [row["start"] for row in expected["calendar"]],
        "actor": ACTOR, "treatment_ids": list(treatment_ids), "text": _allowed_text(expected),
    }
    allowed_env = ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
    inherited = {key: os.environ[key] for key in allowed_env if key in os.environ}
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(__file__).with_name("candidate_probe.py").resolve())],
            cwd=directory, env=inherited, input=json.dumps(payload), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=180,
        )
        result = json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise AssertionError(f"{mode} candidate subprocess did not yield a private report ({type(exc).__name__})") from None
    require(isinstance(result, dict) and completed.returncode == 0,
            f"{mode} candidate subprocess exited without a valid observation")
    return result


def _route_differences(mode, result):
    expected_v3 = mode == "v3"
    expected_auth = {route: "subject_query" if expected_v3 else "subject_digest" for route in COMMON}
    if expected_v3:
        expected_auth.update(dict.fromkeys(NEGOTIATION, "none"))
        expected_auth.update(dict.fromkeys(V3_PROTECTED, "bearer"))
    else:
        expected_auth.update(dict.fromkeys(LEGACY, "subject_digest"))
    records = {tuple(row) for row in result.get("routes", [])}
    missing = {(route, 200, auth) for route, auth in expected_auth.items()} - records
    unexpected = {(route, status, auth) for route, status, auth in records if expected_auth.get(route) != auth}
    differences = []
    if missing:
        differences.append(f"missing successful expected routes {sorted(missing)}")
    if unexpected:
        differences.append(f"unexpected routes or authentication {sorted(unexpected)}")
    expected_header = not expected_v3
    if result.get("uses_v3") is not expected_v3 or result.get("uses_legacy_header") is not expected_header:
        differences.append("native coordinator and client mode flags differ from selected mode")
    return differences


def _keyed(rows, key):
    """Preserve duplicates per identity so missing, extra and repeated rows remain distinguishable."""
    keyed = {}
    for row in rows:
        keyed.setdefault(row[key], []).append(row)
    return keyed


def _indexed(observation):
    copied = dict(observation)
    for section, key in (("treatments", "id"), ("calendar", "start")):
        if isinstance(copied.get(section), list):
            copied[section] = _keyed(copied[section], key)
    if isinstance(copied.get("history"), dict):
        history = copied["history"]
        copied["history"] = {**history, "readings": _keyed(history["readings"], "id")}
    return copied


def _compare(expected, actual, path):
    if isinstance(expected, dict) and isinstance(actual, dict):
        result = [f"{path}.{key}: missing" for key in sorted(expected.keys() - actual.keys())]
        result.extend(f"{path}.{key}: extra" for key in sorted(actual.keys() - expected.keys()))
        for key in sorted(expected.keys() & actual.keys()):
            result.extend(_compare(expected[key], actual[key], f"{path}.{key}"))
        return result
    if isinstance(expected, list) and isinstance(actual, list):
        result = []
        if len(expected) != len(actual):
            result.append(f"{path}: expected {len(expected)} row(s), got {len(actual)}")
        for index, (want, got) in enumerate(zip(expected, actual)):
            result.extend(_compare(want, got, f"{path}[{index}]"))
        return result
    if path.startswith(("observation.states.", "paired.states.")) and path.endswith(".value"):
        if type(expected) in (int, float) and type(actual) in (int, float):
            if math.isclose(expected, actual, abs_tol=1e-9, rel_tol=1e-7):
                return []
    return [] if expected == actual else [f"{path}: expected {expected!r}, got {actual!r}"]


def _fence(reader):
    status, _, body = request("/api/v3/lastModified", headers=reader.headers)
    require(status == 200 and isinstance(body, dict) and body.get("status") == 200,
            "candidate stock storage fence is unavailable")
    clocks = body["result"]["collections"]
    tracked = {key: clocks.get(key) for key in ("entries", "treatments", "devicestatus", "profile")}
    require(all(type(value) is int and value > 0 for value in tracked.values()),
            "candidate stock storage fence must contain live collection clocks")
    return tracked


async def compare_candidate_modes(hass, stock, stock_versions, stock_wire, record_property, tmp_path):
    """Run both children in the same fixture lifetime and compare each with its own stock wire."""
    contract, name, anchor, docs, _ = stock_versions
    root, sha = await hass.async_add_executor_job(_checkout)
    record_property("candidate_commit", sha)
    record_property("candidate_clock_utc", anchor.isoformat())
    reader = await hass.async_add_executor_job(read_subject, stock, name, contract)
    await hass.async_add_executor_job(_verify_header_scope, reader)
    record_property("candidate_permissions", "same named readable subject; legacy digest and v3 bearer")
    before = await hass.async_add_executor_job(_fence, reader)
    calendar_range = (anchor - timedelta(minutes=75), anchor + timedelta(minutes=40))
    history_range = (anchor - timedelta(days=6), anchor + timedelta(seconds=30))
    treatment_ids = [row["_id"] for row in docs["treatments"]]
    failures = []
    observed = {}
    sources_by_mode = {}
    successful_routes = {}
    for mode, api in (("legacy", 1), ("v3", 3)):
        expected = _expected(api, anchor, stock_wire, calendar_range, history_range)
        try:
            result = await hass.async_add_executor_job(
                _child, tmp_path / mode, root, mode, reader.access, anchor, expected,
                calendar_range, history_range, treatment_ids,
            )
        except AssertionError as exc:
            failures.append(str(exc))
            continue
        record_property(f"candidate_{mode}_routes", json.dumps(result.get("routes", [])))
        differences = _route_differences(mode, result)
        if result.get("pytest_exit") != 0 or result.get("phase") != "complete":
            differences.append(
                f"HA probe phase {result.get('phase')} (pytest={result.get('pytest_exit')}, "
                f"launcher={result.get('launcher_error')})"
            )
        else:
            sources = result["sources"]
            sources_by_mode[mode] = sources
            record_property(f"candidate_{mode}_loaded_sources", json.dumps(sources, sort_keys=True))
            known = COMMON | LEGACY | NEGOTIATION | V3_PROTECTED
            successful_routes[mode] = sorted({route for route, status, _ in result["routes"]
                                              if status == 200 and route in known})
            observation = _indexed(result["observation"])
            observed[mode] = observation
            differences.extend(_compare(_indexed(expected), observation, "observation"))
        failures.extend(f"{mode}: {difference}" for difference in differences)
    if observed.keys() == {"legacy", "v3"}:
        legacy_sources, v3_sources = sources_by_mode["legacy"], sources_by_mode["v3"]
        if any(legacy_sources[path] != v3_sources[path] for path in legacy_sources.keys() & v3_sources.keys()):
            failures.append("paired children loaded different bytes for shared product modules")
        # Explicit 0 and absent treatment amounts can differ on the raw wire. HA-facing values agree.
        for section in ("raw_current", "states", "history", "calendar"):
            failures.extend(
                _compare(observed["legacy"][section], observed["v3"][section], f"paired.{section}")
            )
    after = await hass.async_add_executor_job(_fence, reader)
    if after != before:
        failures.append("stock collection clocks changed during the read-only paired observation")
    if successful_routes:
        summary = "; ".join(f"{mode} {' '.join(routes)}" for mode, routes in successful_routes.items())
        receipt = f"Paired HA candidate {sha} at {anchor.isoformat()} | {summary}"
        print(receipt)
        if summary_file := os.environ.get("GITHUB_STEP_SUMMARY"):
            # Only validated revision, time and fixed route labels reach the job summary.
            with Path(summary_file).open("a", encoding="utf-8") as stream:
                stream.write(receipt + "\n")
    require(not failures, "candidate product parity differences:\n" + "\n".join(failures))
