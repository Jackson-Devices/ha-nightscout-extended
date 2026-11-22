#!/usr/bin/env python3
"""Qualify one pinned stock Nightscout against isolated real-HA profiles.

Run from the QA checkout with separately staged product trees and HA Python:
python tests_contract/stock/run_stock.py --upstream PATH --candidate PATH \
    --ha-python PATH --output PATH

Append --legacy-repair PATH to compare a separate repaired legacy checkout
under the same anonymous role, fixture, and observation time.

Output is a credential-free JSON file. Exit 0 means all gates passed, 1 means
a schema or observed product mismatch, and 2 means a native or harness failure.

Stage the exact schema validator wheels for the controller Python:
python -m pip download --only-binary=:all: --require-hashes \
    -r tests_contract/stock/swagger_gate.requirements.txt -d WHEELHOUSE
python -m pip install --no-index --find-links WHEELHOUSE --only-binary=:all: \
    --require-hashes -r tests_contract/stock/swagger_gate.requirements.txt

The runner takes the pinned Swagger JSON bytes from the running stock image.
Stage the locked Docker images beforehand for an offline run.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

import stock_native as raw
from bootstrap import read_lock


STOCK = Path(__file__).resolve().parent
ROOT = STOCK.parents[1]
SCHEMA_VERSION = 1
PROFILES = ("upstream_legacy", "candidate_legacy", "candidate_v3")
PROBE_EXIT = {"pass": 0, "mismatch": 1, "harness_error": 2}


def bare_env():
    env = {"PATH": os.environ.get("PATH", os.defpath), "TZ": "UTC", "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    for name in ("HOME", "USERPROFILE", "SYSTEMROOT", "TMP", "TEMP", "DOCKER_HOST", "DOCKER_CONTEXT"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


class DisposableStock:
    """One unique Compose project, including its temporary Mongo volume."""

    def __init__(self, lock):
        secret = secrets.token_hex(24)
        self.admin = hashlib.sha1(secret.encode("ascii")).hexdigest()
        self.env = {
            **bare_env(),
            "H2_MONGO_IMAGE": lock["server"]["mongo"],
            "H2_NIGHTSCOUT_IMAGE": lock["server"]["nightscout"],
            "H2_NIGHTSCOUT_SECRET": secret,
            "H2_NIGHTSCOUT_DEFAULT_ROLE": "readable",
            "H2_NIGHTSCOUT_LOG_DRIVER": "none",
        }
        self.project = f"nightscout-stock-{secrets.token_hex(6)}"
        self.command = [
            "docker", "compose", "--project-name", self.project, "--env-file", os.devnull,
            "--file", str(raw.HARNESS / "server-compose.yml"), "--file", str(STOCK / "stock-compose.yml"),
        ]
        self.started = False

    def call(self, phase, *arguments, data=None, timeout=120, binary=False):
        command = [*self.command, *arguments]
        try:
            result = subprocess.run(
                command, cwd=ROOT, env=self.env, input=data, encoding=None if binary else "utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise raw.StockError(f"{phase}:{type(exc).__name__}") from None
        raw.check(result.returncode == 0, f"{phase}:exit_{result.returncode}")
        return result.stdout if binary else result.stdout.strip()

    def start(self):
        self.call("compose_pull", "pull", "--policy", "missing", timeout=300)
        self.started = True
        self.call("mongo_start", "up", "--wait", "--wait-timeout", "75", "--pull", "never", "mongo")
        # A clean Mongo import precedes the one and only stock server startup.
        anchor = datetime.now(UTC).replace(second=0, microsecond=0)
        fixture = raw.seed(anchor)
        for collection, rows in fixture.items():
            self.call(
                f"seed_{collection}", "exec", "-T", "mongo", "mongoimport", "--db", "nightscout",
                "--collection", collection, "--jsonArray", data=raw.canonical(rows),
            )
        self.call("nightscout_start", "up", "-d", "--pull", "never", "nightscout")
        address = self.call("nightscout_port", "port", "nightscout", "1337")
        match = re.fullmatch(r"127\.0\.0\.1:(\d{1,5})", address)
        raw.check(match and 0 < int(match[1]) < 65_536, "non_loopback_compose_binding")
        return int(match[1]), anchor, fixture

    def close(self):
        if self.started:
            self.call("compose_cleanup", "down", "--volumes", "--remove-orphans")
            return "pass"
        return "not_started"

    def schemas(self, directory, sources):
        paths = {}
        for api, source in sources.items():
            content = self.call(f"swagger_{api}_extract", "exec", "-T", "nightscout", "cat",
                                f"/opt/app/{source['path']}", binary=True)
            raw.check(hashlib.sha256(content).hexdigest() == source["sha256"], f"swagger_{api}_image_digest")
            paths[api] = directory / f"{api}.json"
            paths[api].write_bytes(content)
        return paths


def child_record(executable, product, mode, url, credential, expected, observed_at, prohibited):
    request = {"url": url, "credential": credential, "mode": mode, "product_root": str(product),
               "observed_at": observed_at, "expected": expected}
    env = {key: value for key, value in bare_env().items() if key not in ("DOCKER_HOST", "DOCKER_CONTEXT")}
    started = time.monotonic()
    try:
        result = subprocess.run(
            [str(executable), str(STOCK / "ha_probe.py")],
            cwd=ROOT, env=env, input=raw.canonical(request), encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=110, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"mode": mode, "status": "harness_error", "phase": type(exc).__name__}
    try:
        # Keep unstructured stdout private even when the child fails. Also scan
        # canonical JSON to catch secrets originally escaped inside its output.
        data = json.loads(result.stdout)
        contents = raw.canonical(data)
        if any(value and (value in contents or value in result.stdout) for value in prohibited):
            return {"mode": mode, "status": "harness_error", "phase": "child_protected_output"}
        raw.check(type(data) is dict and data.get("schema_version") == SCHEMA_VERSION
                  and data.get("mode") == mode and result.returncode == PROBE_EXIT.get(data.get("status"))
                  and isinstance(data.get("actual"), dict) and isinstance(data.get("attestation"), dict)
                  and isinstance(data.get("mismatches"), list), "invalid_probe_receipt")
    except (TypeError, ValueError, raw.StockError):
        return {"mode": mode, "status": "harness_error", "phase": "invalid_probe_receipt",
                "exit_code": result.returncode}
    return {**data, "exit_code": result.returncode, "wall_seconds": round(time.monotonic() - started, 3)}


def run(args, report):
    lock, lock_hash = read_lock()
    raw.check(lock["server"]["nightscout"] == raw.IMAGE, "unexpected_stock_image_in_lock")
    raw.check((STOCK / "ha_probe.py").is_file() and args.ha_python.is_file(), "missing_ha_probe_or_python")
    roots = [args.upstream, args.candidate]
    if args.legacy_repair is not None:
        roots.append(args.legacy_repair)
    raw.check(all((tree / "custom_components").is_dir() for tree in roots),
              "missing_staged_product")
    raw.check(len(set(roots)) == len(roots), "same_staged_product_root")
    report["provider"] = {"version": raw.VERSION, "upstream_commit": raw.SOURCE,
                          "images": lock["server"], "lock_sha256": lock_hash}
    report["schema"] = {"status": "harness_error", "phase": "swagger_validator_dependency"}
    from swagger_gate import SCHEMA_SOURCES, STOCK_COMMIT, SwaggerGate, SwaggerGateSetupError

    raw.check(STOCK_COMMIT == raw.SOURCE and set(SCHEMA_SOURCES) == {"v1", "v3"}, "swagger_source_pin")
    report["schema"] = {"status": "harness_error", "phase": "swagger_image_setup", "sources": SCHEMA_SOURCES}
    disposable = DisposableStock(lock)
    try:
        port, anchor, fixture = disposable.start()
        report["fixture"] = {"data_sha256": raw.digest(fixture), "observed_at": raw.utc(anchor),
                             "origin": "single 127.0.0.1 Compose binding", "seed_imports": list(fixture)}
        with tempfile.TemporaryDirectory(prefix="stock-swagger-") as directory:
            paths = disposable.schemas(Path(directory), SCHEMA_SOURCES)
            try:
                gate = SwaggerGate(paths["v1"], paths["v3"])
            except SwaggerGateSetupError:
                raise raw.StockError("swagger_validator_setup") from None
            wire = raw.ObservedGets(port, gate, SCHEMA_SOURCES)
            try:
                status = read_and_compare(args, report, disposable, port, anchor, fixture, wire)
            finally:
                report["schema"] = wire.receipt()
            if "harness_error" in (status, report["schema"]["status"]):
                return "harness_error"
            if "mismatch" in (status, report["schema"]["status"]):
                return "mismatch"
            return "pass"
    finally:
        try:
            report["cleanup"] = disposable.close()
        except raw.StockError as exc:
            report["cleanup"] = "harness_error"
            report["cleanup_phase"] = str(exc)


def read_and_compare(args, report, disposable, port, anchor, fixture, wire):
    credential, reader, _, _ = raw.native_subject(port, disposable.admin, name="stock-parity-reader")
    raw.ready(wire, reader, fixture["entries"][-1])
    report["effective_permissions"] = raw.permission_matrix(port, reader, wire)
    before = report["native_before"] = raw.snapshot(wire, reader, fixture, anchor)
    expected = report["expected"] = raw.expected_for_ha(before, anchor)
    origin = f"http://127.0.0.1:{port}"
    secret_strings = (credential, reader["Authorization"], reader["Authorization"].split(" ", 1)[1],
                      disposable.admin, disposable.env["H2_NIGHTSCOUT_SECRET"])
    results = report["profiles"] = {}
    for mode in PROFILES:
        subject = credential if mode == "candidate_v3" else None
        tree = args.upstream if mode == "upstream_legacy" else args.candidate
        results[mode] = child_record(args.ha_python, tree, mode, origin, subject, expected,
                                     raw.utc(anchor), secret_strings)
    if args.legacy_repair is not None:
        results["repaired_legacy"] = repaired_legacy(
            args.ha_python, args.legacy_repair, origin, expected, raw.utc(anchor), secret_strings,
        )
    after = raw.snapshot(wire, reader, fixture, anchor)
    stable = raw.stable_view(before) == raw.stable_view(after)
    report["native_stability"] = {
        "status": "pass" if stable else "mismatch",
        "before_sha256": raw.digest(raw.stable_view(before)), "after_sha256": raw.digest(raw.stable_view(after)),
        "after_storage_fence": after["storage_fence"],
    }
    raw.check(stable, "stock_snapshot_changed_during_ha_profiles")
    report["late_history"] = raw.late_history(port, wire, reader, disposable.admin, after, anchor)
    outcomes = {result["status"] for result in results.values()}
    if "harness_error" in outcomes:
        return "harness_error"
    native_shape_failed = bool(before["treatment_wire_source"]["fixture_shape_differences"])
    return "mismatch" if native_shape_failed or "mismatch" in outcomes else "pass"


def repaired_legacy(executable, product, origin, expected, observed_at, prohibited):
    component = product / "custom_components" / "nightscout_extended"
    try:
        manifest = hashlib.sha256((component / "manifest.json").read_bytes()).hexdigest()
        calendar = hashlib.sha256((component / "calendar.py").read_bytes()).hexdigest()
    except OSError:
        return {"mode": "upstream_legacy", "status": "harness_error", "phase": "repair_source_unreadable"}

    result = child_record(executable, product, "upstream_legacy", origin, None, expected, observed_at, prohibited)
    attestation = result.setdefault("attestation", {})
    loaded = attestation.get("source_files_sha256")
    matched = (attestation.get("exact_product_root") is True
               and attestation.get("exact_product_package") is True
               and attestation.get("manifest_sha256") == manifest
               and isinstance(loaded, dict)
               and loaded.get("calendar.py") == calendar)
    attestation["designated_checkout"] = {
        "resolved_root_sha256": hashlib.sha256(os.fsencode(product)).hexdigest(),
        "manifest_sha256": manifest, "calendar_sha256": calendar, "matches_loaded_source": matched,
    }
    if result["status"] == "pass" and not matched:
        result.update(status="harness_error", phase="repair_source_attestation")
    return result


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.name}.",
                                     delete=False, encoding="utf-8", newline="\n") as stream:
        temp = Path(stream.name)
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--legacy-repair", type=Path)
    parser.add_argument("--ha-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.upstream = args.upstream.resolve()
    args.candidate = args.candidate.resolve()
    if args.legacy_repair is not None:
        args.legacy_repair = args.legacy_repair.resolve()
    # Preserve the chosen virtual environment's executable symlink.
    args.ha_python = args.ha_python.absolute()
    report = {"schema_version": SCHEMA_VERSION}
    started = time.monotonic()
    try:
        report["status"] = run(args, report)
    except Exception as exc:
        report["status"] = "harness_error"
        report["phase"] = str(exc) if isinstance(exc, raw.StockError) else type(exc).__name__
    if report.get("cleanup") == "harness_error":
        report["status"] = "harness_error"
    report["wall_seconds"] = round(time.monotonic() - started, 3)
    try:
        save(args.output, report)
    except (OSError, ValueError):
        print("stock parity: report_write_failure")
        return 2
    print(f"stock parity: {report['status']}")
    return PROBE_EXIT[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
