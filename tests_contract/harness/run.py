"""Run Nightscout Extended checks using the state staged by bootstrap.py.

    python3 tests_contract/harness/run.py core --state /tmp/nightscout-h2 \
        --runner-id codex-clean --output /tmp/nightscout-h2-codex-clean
    python3 tests_contract/harness/run.py server --state /tmp/nightscout-h2 \
        --runner-id codex-server --output /tmp/nightscout-h2-codex-server

Use separate output paths for clean and repeated core runs. A baseline comparison
can accept an exact expected list of failed JUnit case IDs. Core is strict by
default. The server command requires Docker Compose; use --pull to stage images. Core
uses the staged environments and starts only the loopback contract fixture.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener
import xml.etree.ElementTree as ET

from bootstrap import HARNESS, clean_env, read_lock

ROOT = HARNESS.parents[1]
CONTRACT = ROOT / "tests_contract" / "contract" / "run.py"
ADAPTER = "tests_contract/adapter/test_upstream_checks.py"
POSITIVE_NODE = f"{ADAPTER}::test_upstream_setup_and_lifecycle"
CONTRACT_NODE = f"{ADAPTER}::test_frozen_contract"
FORECAST_MODULE = "tests_contract/adapter/test_forecast_compatibility.py"
PYTEST_HA = ["-q", "-c", "tests_ha/pytest.ini", "-p", "no:cacheprovider"]


def core_env():
    env = clean_env()
    env.update(UV_OFFLINE="1", PIP_NO_INDEX="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def step(output, name, command, *, pytest=False, expected_exit=0, baseline=False):
    start = time.monotonic()
    log = output / f"{name}.log"
    report = output / f"{name}.xml"
    result = {"name": name, "log": log.name, "command": [str(part) for part in command]}
    if pytest:
        command = [*command, f"--junitxml={report}"]
        result["junit"] = report.name
    try:
        with log.open("wb") as stream:
            completed = subprocess.run(
                command, cwd=ROOT, env=core_env(), stdout=stream, stderr=subprocess.STDOUT,
                check=False, timeout=300,
            )
        code = completed.returncode
        result["exit_code"] = code
        result["status"] = "passed" if code == expected_exit else "infrastructure_failure"
        if pytest:
            cases = ET.parse(report).getroot().iter("testcase")
            failures = []
            errors = []
            for case in cases:
                identity = f"{case.get('classname')}::{case.get('name')}"
                if case.find("error") is not None:
                    errors.append(identity)
                failure = case.find("failure")
                if failure is not None:
                    detail = f"{failure.get('message', '')} {failure.text or ''}"
                    if baseline and ("HARNESS " in detail or "BASELINE_MISMATCH " not in detail):
                        errors.append(identity)
                    else:
                        failures.append(identity)
            result.update(failed_cases=failures, errored_cases=errors)
            if errors:
                result["status"] = "infrastructure_failure"
            elif code == 1 and failures:
                result["status"] = "product_mismatch"
    except (OSError, ET.ParseError, subprocess.SubprocessError) as exc:
        result.update(status="infrastructure_failure", error=str(exc))
    result["seconds"] = round(time.monotonic() - start, 3)
    return result


def core(args, results):
    parser = args.state / "parser" / "bin" / "python"
    ha = args.state / "ha" / "bin" / "python"
    tests_js = sorted((ROOT / "tests_js").glob("*.test.mjs"))
    commands = [
        ("tests", [parser, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"], True),
        ("tests_js", [args.state / "bin" / "node", "--test", *tests_js], False),
        ("tests_ha", [ha, "-m", "pytest", "tests_ha", *PYTEST_HA], True),
    ]
    for name, command, pytest in commands:
        if name == "tests_js" and not tests_js:
            results.append({"name": name, "status": "infrastructure_failure", "error": "No card tests found"})
            continue
        result = step(args.output, name, command, pytest=pytest)
        if name == "tests_js" and result.get("exit_code") == 1 and "ERR_ASSERTION" in (
            args.output / result["log"]
        ).read_text(encoding="utf-8", errors="replace"):
            result["status"] = "product_mismatch"
        results.append(result)

    controls = (
        ("good", 0, r"^PASS [1-9]"),
        ("wrong-route", 1, r"^FAIL .*: wire "),
        ("wrong-state", 1, r"^FAIL (?!.*: wire )"),
        ("broken-environment", 2, r"^HARNESS NotADirectoryError:"),
    )
    for control, status, marker in controls:
        outcome = step(args.output, f"control-{control}", [parser, CONTRACT, control], expected_exit=status)
        if outcome["status"] == "passed":
            log = (args.output / outcome["log"]).read_text(encoding="utf-8", errors="replace")
            if not re.search(marker, log, re.MULTILINE):
                outcome.update(status="infrastructure_failure", error="Expected control diagnostic missing")
        results.append(outcome)

    results.append(step(
        args.output, "ha-positive",
        [ha, "-m", "pytest", POSITIVE_NODE, FORECAST_MODULE, *PYTEST_HA], pytest=True,
    ))
    observer = step(
        args.output, "ha-contract", [ha, "-m", "pytest", CONTRACT_NODE, *PYTEST_HA], pytest=True, baseline=True,
    )
    results.append(observer)
    if args.expected_failures:
        expected = json.loads(args.expected_failures.read_text(encoding="utf-8"))
        if not isinstance(expected, list) or not all(isinstance(case, str) for case in expected):
            raise ValueError("--expected-failures must be a JSON list of JUnit classname::name strings")
        actual = sorted(observer.get("failed_cases", []))
        observer["expected_failed_cases"] = sorted(expected)
        if observer["status"] == "product_mismatch" and actual == sorted(expected):
            observer["status"] = "expected_baseline_difference"
        elif observer["status"] == "passed" and expected:
            observer["status"] = "product_mismatch"


def seed_entries(anchor, seed):
    entries = []
    for entry in seed["entries"]:
        when = anchor - timedelta(minutes=entry["minutes_before_anchor"])
        entries.append({
            "type": "sgv", "sgv": entry["sgv"], "direction": entry["direction"],
            "date": int(when.timestamp() * 1000), "dateString": when.isoformat(), "device": seed["device"],
        })
    return entries


def server_json(url, path, deadline):
    timeout = min(5, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError("Nightscout readiness deadline expired")
    opener = build_opener(ProxyHandler({}))
    with opener.open(url + path, timeout=timeout) as response:
        return json.load(response)


def loopback_server(lock, entries, runner, results, pull):
    project = f"nightscout-h2-{secrets.token_hex(6)}"
    env = clean_env()
    env.update(
        H2_MONGO_IMAGE=lock["server"]["mongo"], H2_NIGHTSCOUT_IMAGE=lock["server"]["nightscout"],
        H2_NIGHTSCOUT_SECRET=secrets.token_hex(24),
    )
    command = ["docker", "compose", "--project-name", project, "--file", HARNESS / "server-compose.yml"]

    def compose(*args, data=None):
        return subprocess.run(
            [*command, *args], cwd=ROOT, env=env, input=data, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=True,
        ).stdout.strip()

    requested_startup = False
    try:
        if pull:
            compose("pull")
            results.append({"name": "server-images", "status": "passed"})
        requested_startup = True
        compose("up", "--wait", "--wait-timeout", "75", "--pull", "never", "mongo")
        compose(
            "exec", "-T", "mongo", "mongoimport", "--db", "nightscout", "--collection", "entries", "--jsonArray",
            data=json.dumps(entries),
        )
        compose("up", "-d", "--pull", "never", "nightscout")
        address = compose("port", "nightscout", "1337")
        if not re.fullmatch(r"127\.0\.0\.1:\d+", address):
            raise ValueError("The disposable Nightscout port was not bound to loopback")
        runner(f"http://{address}")
    finally:
        if requested_startup:
            try:
                compose("down", "--volumes", "--remove-orphans")
            except (OSError, subprocess.SubprocessError) as exc:
                results.append({"name": "server-cleanup", "status": "infrastructure_failure", "error": type(exc).__name__})


def server(args, lock, results):
    anchor = datetime.fromisoformat(args.anchor) if args.anchor else datetime.now(timezone.utc).replace(microsecond=0)
    if anchor.tzinfo is None:
        raise ValueError("--anchor requires a UTC offset")
    seed = json.loads((HARNESS / "seed.json").read_text(encoding="utf-8"))
    entries = seed_entries(anchor, seed)
    latest = entries[-1]

    def replay(url):
        deadline = time.monotonic() + 60
        seed_seen = False
        while time.monotonic() < deadline:
            try:
                status = server_json(url, "/api/v1/status.json", deadline)
                records = server_json(url, "/api/v1/entries/sgv.json?count=2", deadline)
                if isinstance(status, dict) and status.get("version") and isinstance(records, list) and any(
                    item.get("date") == latest["date"] and item.get("sgv") == latest["sgv"]
                    and item.get("device") == seed["device"] for item in records if isinstance(item, dict)
                ):
                    seed_seen = True
                    properties = server_json(url, "/api/v2/properties/bgnow", deadline)
                    bgnow = properties.get("bgnow") if isinstance(properties, dict) else None
                    if isinstance(bgnow, dict) and (bgnow.get("last"), bgnow.get("mills")) == (
                        latest["sgv"], latest["date"],
                    ) and time.monotonic() < deadline:
                        break
            except (OSError, ValueError):
                pass
            time.sleep(max(0, min(1, deadline - time.monotonic())))
        else:
            reason = (
                "Nightscout did not calculate glucose and time for the expected synthetic seed"
                if seed_seen else "Nightscout did not return the expected synthetic seed"
            )
            results.append({"name": "nightscout-replay", "status": "infrastructure_failure", "error": reason})
            return
        results.append({
            "name": "nightscout-replay", "status": "passed", "version": status["version"],
            "seed": latest, "bgnow": {"last": bgnow["last"], "mills": bgnow["mills"]},
        })
        ha = args.state / "ha" / "bin" / "python"
        result = step(args.output, "nightscout-client", [ha, Path(__file__).resolve(), "_server-client", url,
                                                          str(latest["sgv"]), str(latest["date"])])
        if result.get("exit_code") == 1:
            result["status"] = "product_mismatch"
        results.append(result)

    if args.url:
        if not re.fullmatch(r"https?://[a-zA-Z0-9.-]+(?::\d+)?", args.url):
            raise ValueError("--url must be a disposable Nightscout base URL without credentials or a path")
        replay(args.url)
    else:
        loopback_server(lock, entries, replay, results, args.pull)


async def check_client(url, glucose, millis):
    sys.path.insert(0, str(ROOT))
    from aiohttp import ClientSession, ClientTimeout
    from custom_components.nightscout_extended.api import NightscoutClient, NightscoutConnectionError

    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        try:
            data = await NightscoutClient(session, url, None).async_get_data()
        except NightscoutConnectionError:
            print("Nightscout client did not read the working disposable server")
            return 1
    actual_time = int(data.glucose_time.timestamp() * 1000) if data.glucose_time else None
    print(json.dumps({"actual_glucose": data.glucose, "actual_date": actual_time}))
    return 0 if (data.glucose, actual_time) == (int(glucose), int(millis)) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="lane", required=True)
    core_args = commands.add_parser("core", help="Run the staged offline core checks")
    core_args.add_argument("--expected-failures", type=Path, help="Allow an exact known HA baseline case set")
    server_args = commands.add_parser("server", help="Seed and check a separate disposable Nightscout server")
    server_args.add_argument("--pull", action="store_true", help="Stage the pinned disposable images before starting")
    server_args.add_argument("--url", help="Read-only base URL of a reachable disposable server already seeded")
    server_args.add_argument("--anchor", help="Seed anchor in ISO 8601 with offset, required with --url")
    for command in (core_args, server_args):
        command.add_argument("--state", required=True, type=Path)
        command.add_argument("--runner-id", required=True)
        command.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.lane == "server" and args.url and (not args.anchor or args.pull):
        server_args.error("--url requires --anchor and cannot be used with --pull")
    args.output = args.output.resolve()
    args.state = args.state.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    digest = None
    try:
        lock, digest = read_lock()
        receipt = json.loads((args.state / "ready.json").read_text(encoding="utf-8"))
        if receipt["lock_sha256"] != digest:
            raise ValueError("State uses a different dependency lock. Run bootstrap.py with this checkout.")
        if args.lane == "core":
            core(args, results)
        else:
            server(args, lock, results)
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        # Compose child output can contain its generated local API secret.
        detail = type(exc).__name__ if args.lane == "server" else str(exc)
        results.append({"name": args.lane + "-setup", "status": "infrastructure_failure", "error": detail})
    receipt = {
        "runner_id": args.runner_id, "lane": args.lane, "lock_sha256": digest, "results": results,
    }
    (args.output / "results.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(args.output / "results.json")
    states = {result["status"] for result in results}
    if "infrastructure_failure" in states:
        return 2
    return 1 if "product_mismatch" in states else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["_server-client"]:
        raise SystemExit(asyncio.run(check_client(*sys.argv[2:])))
    raise SystemExit(main())
