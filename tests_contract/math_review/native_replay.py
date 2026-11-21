"""Replay fixed synthetic SGVs through disposable Nightscout, HA, and the bundled card.

Run from an assembled QA checkout prepared with math_review/stage.py:

    python3 tests_contract/math_review/native_replay.py --state "$STATE" --output "$NATIVE_OUT"

Add --pull to stage the pinned public images if absent. The output directory must
be new. All cases share one real UTC anchor. The HA and browser clock inputs
are explicit and the provider's reported wall clock remains a separate fact.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests_contract" / "harness"
TEST = "tests_contract/math_review/test_native_math.py"
ENABLE = "NSX_MATH_NATIVE_REPLAY"
PROPERTIES = "/api/v2/properties/bgnow,delta,ar2"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
THRESHOLDS = {"bgLow": 55, "bgTargetBottom": 80, "bgTargetTop": 180, "bgHigh": 260}
CASES = {
    "NATIVE-HIGH-EQUAL-AR2": {
        "values": (170, 180), "forecast": True, "mmol_display": "10.0",
    },
    "NATIVE-LOW-EQUAL-NO-AR2": {
        "values": (80,), "forecast": False, "mmol_display": "4.4",
    },
}
MODES = ("public", "protected")


class ReplayError(Exception):
    """A replay prerequisite failed with a fixed, non-secret phase code."""


def is_loopback(url):
    """Accept the exact disposable Compose endpoint shape."""
    match = re.fullmatch(r"http://127\.0\.0\.1:(\d{1,5})", url)
    return bool(match and 0 < int(match[1]) < 65_536)


def instant(ms):
    return EPOCH + timedelta(milliseconds=ms)


def epoch_ms(value):
    return (datetime.fromisoformat(value.replace("Z", "+00:00")) - EPOCH) // timedelta(milliseconds=1)


def write_observation(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def seed(case, poll_ms):
    spec = CASES[case]
    values = spec["values"]
    last_ms = poll_ms - 60_000 + 7
    readings = [
        {
            "id": f"{0x650000000000000000000000 + index + 1:024x}",
            "date": last_ms - (len(values) - index - 1) * 300_000,
            "sgv": value,
            "direction": "FortyFiveUp" if index else "Flat",
        }
        for index, value in enumerate(values)
    ]
    documents = [
        {
            "_id": {"$oid": row["id"]},
            "device": "math-review-synthetic",
            "type": "sgv",
            "date": row["date"],
            "dateString": instant(row["date"]).isoformat(timespec="milliseconds"),
            "sgv": row["sgv"],
            "direction": row["direction"],
        }
        for row in readings
    ]
    return documents, {
        "case": case,
        "poll_ms": poll_ms,
        "browser_ms": poll_ms + 31_000,
        "readings": readings,
        "forecast_expected": spec["forecast"],
        "mmol_display": spec["mmol_display"],
    }


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _selector(mapping, fields):
    return {key: mapping.get(key) for key in fields}


def _site_paths(access):
    query = f"?{urlencode({'token': access})}" if access else ""
    return f"/api/v1/status.json{query}", f"{PROPERTIES}{query}"


def _raw_readings(rows):
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ReplayError("invalid_native_rows")
        ident = row.get("identifier") or row.get("_id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{24}", ident):
            raise ReplayError("invalid_native_identity")
        if type(row.get("date")) is not int or not _number(row.get("sgv")):
            raise ReplayError("invalid_native_measurement")
        direction = row.get("direction")
        if direction not in ("Flat", "FortyFiveUp"):
            raise ReplayError("unexpected_native_direction")
        result.append({
            "id": ident, "date": row["date"], "sgv": row["sgv"], "direction": direction,
        })
    return sorted(result, key=lambda row: (row["date"], row["id"]))


def native_observation(server, port, fixture, *, access=None, bearer=None):
    """Capture only allowlisted synthetic measurements and native calculations."""
    status_path, properties_path = _site_paths(access)
    status = server.await_read(
        lambda: server.request(port, status_path),
        lambda value: isinstance(value, dict) and isinstance(value.get("settings"), dict),
        "native math status",
    )
    thresholds = _selector(_mapping(status["settings"].get("thresholds")), THRESHOLDS)
    if status.get("version") != "15.0.6" or thresholds != THRESHOLDS:
        raise ReplayError("unexpected_pinned_version_or_thresholds")
    if type(status.get("serverTimeEpoch")) is not int:
        raise ReplayError("missing_provider_clock")

    latest = fixture["readings"][-1]
    if not 0 <= status["serverTimeEpoch"] - latest["date"] <= 15 * 60_000:
        raise ReplayError("native_seed_not_fresh_at_provider")

    def ready(value):
        now = _mapping(_mapping(value).get("bgnow"))
        return (now.get("last"), now.get("mills")) == (latest["sgv"], latest["date"])

    properties = server.await_read(
        lambda: server.request(port, properties_path), ready, "native math source measurement",
    )
    if bearer is not None:
        rows = server.raw_rows(
            port, server.ENTRIES, {"type": "sgv", "sort$desc": "date", "limit": 10}, bearer,
        )
    else:
        code, rows = server.request(port, "/api/v1/entries/sgv.json?count=10")
        if code != 200 or not isinstance(rows, list):
            raise ReplayError("public_native_sgv_response")
    readings = _raw_readings(rows)
    if readings != fixture["readings"]:
        raise ReplayError("native_seed_readback_mismatch")

    current = _selector(properties["bgnow"], ("last", "mills", "mean"))
    delta = _selector(
        _mapping(properties.get("delta")), ("mgdl", "absolute", "elapsedMins", "mean5MinsAgo"),
    )
    native_forecast = _mapping(_mapping(properties.get("ar2")).get("forecast")).get("predicted")
    if native_forecast is not None and not isinstance(native_forecast, list):
        raise ReplayError("invalid_native_forecast_shape")
    points = []
    for point in native_forecast or ():
        if not isinstance(point, dict) or type(point.get("mills")) is not int or not _number(point.get("mgdl")):
            raise ReplayError("invalid_native_forecast_point")
        points.append(_selector(point, ("mills", "mgdl")))
    expected_count = 6 if fixture["forecast_expected"] else 0
    if len(points) != expected_count:
        raise ReplayError("native_forecast_presence_control")
    if len({point["mills"] for point in points}) != len(points):
        raise ReplayError("native_forecast_timestamp_ambiguity")
    return {
        "version": status["version"],
        "server_clock_ms": status["serverTimeEpoch"],
        "thresholds": thresholds,
        "readings": readings,
        "bgnow": current,
        "delta": delta,
        "forecast": points,
        "forecast_wire_shape": "missing" if native_forecast is None else "list",
    }


def _run_child(ha, node, output, fixture, native, mode, url, access):
    observation = output / f"{mode}--{fixture['case']}.json"
    payload = {
        "fixture": fixture, "native": native, "mode": mode, "url": url,
        "subject": access, "node": str(node), "output": str(observation),
    }
    # The one-use subject travels only on private stdin. The HA test's storage
    # fixture keeps config entries in memory. Drop all unstructured child output
    # even if a third-party debug or test hook formats the configuration.
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "TZ": "UTC", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1", ENABLE: "1",
    }
    result = subprocess.run(
        [
            str(ha), "-m", "pytest", "-c", "tests_ha/pytest.ini", "-p", "no:cacheprovider",
            "--capture=no", "--tb=no", TEST,
        ],
        cwd=ROOT, env=env, input=json.dumps(payload, allow_nan=False), encoding="utf-8",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=150, check=False,
    )
    if not observation.is_file():
        return {"status": "infrastructure_failure", "phase": "ha_no_observation", "exit_code": result.returncode}
    record = json.loads(observation.read_text(encoding="utf-8"))
    if record.get("infrastructure_phase") == "forecast_input_ambiguity":
        return {"status": "infrastructure_failure", "phase": "forecast_input_ambiguity",
                "observation": observation.name, "exit_code": result.returncode}
    checks = record.get("checks", {})
    failed = sorted(name for name, passed in checks.items() if passed is False)
    if result.returncode == 0 and checks and all(passed is True for passed in checks.values()):
        status = "passed"
    elif result.returncode == 1 and failed:
        status = "product_mismatch"
    else:
        status = "infrastructure_failure"
    return {"status": status, "observation": observation.name, "failed_checks": failed,
            "exit_code": result.returncode}


def _probe(compose, server, lock, ha, node, output, mode, case, poll_ms):
    documents, fixture = seed(case, poll_ms)
    outcome = {"status": "infrastructure_failure", "phase": "disposable_setup"}
    lifecycle = []

    def read(url, admin=None):
        nonlocal outcome
        if not is_loopback(url):
            raise ReplayError("disposable_server_address")
        port = int(url.rsplit(":", 1)[1])
        access = bearer = subject_id = None
        try:
            if mode == "protected":
                access, bearer, subject_id, _ = server.native_subject(
                    port, admin, name=f"math-reader-{secrets.token_hex(4)}",
                )
                server.require(
                    server.request(port, f"{server.ENTRIES}?limit=1")[0] in (401, 403),
                    "native math anonymous denial",
                )
                for action in ("create", "update", "delete"):
                    permission = quote(f"api:entries:{action}", safe=":")
                    server.require(
                        server.request(port, f"{server.AUTH}/debug/check/{permission}", headers=bearer)[0]
                        in (401, 403), "native math subject is read-only",
                    )
            native = native_observation(server, port, fixture, access=access, bearer=bearer)
            provider_path = output / f"{mode}--{case}.provider.json"
            write_observation(provider_path, {"mode": mode, "fixture": fixture, "native": native})
            outcome = _run_child(ha, node, output, fixture, native, mode, url, access)
            outcome["provider_observation"] = provider_path.name
        finally:
            if subject_id is not None:
                code, _ = server.request(
                    port, f"{server.AUTH}/subjects/{subject_id}",
                    method="DELETE", headers={"api-secret": admin},
                )
                if code != 200:
                    raise ReplayError("disposable_subject_cleanup")

    try:
        compose.loopback_server(
            lock, documents, read, lifecycle, False, protected=(mode == "protected"),
        )
    except ReplayError as exc:
        outcome = {**outcome, "status": "infrastructure_failure", "phase": str(exc)}
    except Exception as exc:
        outcome = {**outcome, "status": "infrastructure_failure", "phase": "native_replay_error",
                   "error_type": type(exc).__name__}
    if any(item.get("status") == "infrastructure_failure" for item in lifecycle):
        outcome = {**outcome, "status": "infrastructure_failure", "phase": "compose_cleanup"}
    return {"mode": mode, "case": case, **outcome}


def _load_harness():
    # Accepted harness modules are designed to import from this path.
    sys.path.insert(0, str(HARNESS))
    import bootstrap
    import run
    import server_v3
    return bootstrap, run, server_v3


def _pull_images(lock):
    # A clean pull may take longer than the synthetic freshness window. Pull
    # the same pinned images before fixing the common measurement timestamps.
    for name in ("mongo", "nightscout"):
        subprocess.run(
            ["docker", "pull", lock["server"][name]], timeout=300, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True, help="staged math-review environment")
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory")
    parser.add_argument("--mode", choices=("both", *MODES), default="both")
    parser.add_argument("--case", choices=tuple(CASES), help="only one synthetic seed")
    parser.add_argument("--pull", action="store_true", help="pull pinned Docker images once")
    args = parser.parse_args()

    if not sys.platform.startswith("linux") or shutil.which("docker") is None:
        parser.exit(2, "Native replay requires Linux and Docker Compose.\n")
    state = args.state.resolve()
    ha, node = state / "ha/bin/python", state / "bin/node"
    if not all(path.is_file() and os.access(path, os.X_OK) for path in (ha, node)):
        parser.exit(2, "Stage the math-review HA interpreter and Node before native replay.\n")
    support = ROOT / "tests_contract" / "math_review"
    if not all((support / name).is_file() for name in ("projection.py", "card_probe.mjs", "oracle.py")):
        parser.exit(2, "Assemble the shared math-review projection and oracle first.\n")

    try:
        bootstrap, compose, server = _load_harness()
        lock, lock_digest = bootstrap.read_lock()
        output = args.output.resolve()
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        cases = (args.case,) if args.case else CASES
        modes = MODES if args.mode == "both" else (args.mode,)
        pairs = [(mode, case) for mode in modes for case in cases]
        if args.pull:
            _pull_images(lock)
        poll_ms = time.time_ns() // 1_000_000_000 * 1000
        results = []
        for mode, case in pairs:
            result = _probe(compose, server, lock, ha, node, output, mode, case, poll_ms)
            results.append(result)
            print(f"{mode} {case}: {result['status']}", flush=True)
        write_observation(output / "summary.json", {
            "poll_ms": poll_ms, "lock_sha256": lock_digest, "results": results,
        })
    except (ImportError, OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"Native replay prerequisites failed ({type(exc).__name__}).\n")
    if any(row["status"] == "infrastructure_failure" for row in results):
        return 2
    return int(any(row["status"] == "product_mismatch" for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
