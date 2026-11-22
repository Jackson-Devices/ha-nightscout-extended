"""Measure branch coverage for the API and Home Assistant Python suites."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


LOCK = Path(__file__).resolve().parents[1] / "harness" / "versions.lock"
COVERAGE_CONFIG = """\
[run]
branch = True
source =
    custom_components/nightscout_extended
"""
COVERAGE_PACKAGES = {"coverage", "pytest-cov"}
SUITE_PLUGIN = "tests_contract.stock.coverage_fixtures"
INHERITED_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL")


def run(command, candidate, env, log):
    with log.open("wb") as stream:
        result = subprocess.run(
            command, cwd=candidate, env=env, stdout=stream, stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode:
        print(f"{log.stem} exited {result.returncode}. See {log}.", file=sys.stderr)
    return result.returncode


def run_suite(command, candidate, env, log, name):
    # Pytest and integration output can include credential-bearing fixture values.
    # The suite plugin writes only test identifiers and structured failure locations.
    log.write_text(f"Starting {name} suite\n", encoding="utf-8")
    result = subprocess.run(
        command, cwd=candidate, env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False,
    )
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"pytest exit: {result.returncode}\n")
    if result.returncode:
        print(f"{name} exited {result.returncode}. See {log}.", file=sys.stderr)
    return result.returncode


def locked_versions():
    requirements = json.loads(LOCK.read_text(encoding="utf-8"))["environments"]["ha"][
        "requirements"
    ]
    pins = {}
    for requirement in requirements:
        name, version = requirement.split(maxsplit=1)[0].split("==", 1)
        if name in COVERAGE_PACKAGES:
            pins[name] = version
    if pins.keys() != COVERAGE_PACKAGES:
        raise ValueError("The HA lock must pin coverage and pytest-cov")
    return pins


def collect(args):
    candidate = args.candidate.resolve()
    # Keep the venv's executable path even when it is a symlink to a base Python.
    python = args.ha_python.absolute()
    output = args.output.resolve()
    suites = (
        ("api", "tests", ["-p", "no:homeassistant"]),
        ("ha", "tests_ha", ["-c", "tests_ha/pytest.ini"]),
    )
    for _, suite, _ in suites:
        if not (candidate / suite).is_dir():
            raise ValueError(f"Candidate has no {suite} suite")
    if not (candidate / "tests_ha" / "pytest.ini").is_file():
        raise ValueError("Candidate has no Home Assistant pytest configuration")
    if not (candidate / "custom_components" / "nightscout_extended").is_dir():
        raise ValueError("Candidate has no Nightscout Extended integration")
    if not (candidate / "tests_contract" / "stock" / "coverage_fixtures.py").is_file():
        raise ValueError("Candidate has no coverage suite plugin")
    if not python.is_file():
        raise ValueError("Home Assistant Python executable does not exist")
    if output == candidate or candidate in output.parents:
        raise ValueError("Output must be scratch space outside the candidate checkout")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output must be a new or empty directory")

    output.mkdir(parents=True, exist_ok=True)
    temp = output / "tmp"
    temp.mkdir()
    config = output / "coveragerc"
    config.write_text(COVERAGE_CONFIG, encoding="utf-8")
    data = output / ".coverage"
    env = {name: os.environ[name] for name in INHERITED_ENV if name in os.environ}
    env.update(
        COVERAGE_FILE=str(data), COVERAGE_RCFILE=str(config),
        PYTHONNOUSERSITE="1", TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp),
    )

    pins = locked_versions()
    check_versions = (
        "from importlib.metadata import version; import json, sys; "
        "pins = json.loads(sys.argv[1]); "
        "actual = {name: version(name) for name in pins}; "
        "print(json.dumps(actual, sort_keys=True)); sys.exit(actual != pins)"
    )
    if run([python, "-I", "-B", "-c", check_versions, json.dumps(pins)],
           candidate, env, output / "versions.log"):
        return 1

    statuses = []
    for name, suite, pytest_config in suites:
        log = output / f"{name}.log"
        suite_env = {
            **env, "COVERAGE_FILE": str(data.with_suffix(f".{name}")),
            "NSX_COVERAGE_SUITE": name, "NSX_COVERAGE_LOG": str(log),
        }
        command = [
            python, "-B", "-m", "pytest", suite, *pytest_config,
            "-q", "-p", "no:cacheprovider", "-p", SUITE_PLUGIN,
            f"--basetemp={temp / name}",
            f"--cov-config={config}", "--cov", "--cov-report=",
        ]
        statuses.append(run_suite(command, candidate, suite_env, log, name))
    if any(statuses):
        return 1

    for name, _, _ in suites:
        if not data.with_suffix(f".{name}").is_file():
            print(f"{name} produced no coverage data", file=sys.stderr)
            return 1

    coverage = [python, "-B", "-m", "coverage"]
    if run([*coverage, "combine", "--keep", output],
           candidate, env, output / "combine.log"):
        return 1
    report = output / "term-missing.txt"
    if run([*coverage, "report", "--show-missing"],
           candidate, env, report):
        return 1
    if run([*coverage, "json", "-o", output / "coverage.json"],
           candidate, env, output / "json.log"):
        return 1
    print(report.read_text(encoding="utf-8"), end="")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True, help="Composed QA checkout")
    parser.add_argument("--ha-python", type=Path, required=True, help="Staged HA interpreter")
    parser.add_argument("--output", type=Path, required=True, help="New or empty scratch directory")
    args = parser.parse_args()
    try:
        return collect(args)
    except (OSError, ValueError) as exc:
        print(f"Coverage setup failed. {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
