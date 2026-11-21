"""Named Nightscout math mutations and a separate bounded mutmut experiment.

Inspect: python3 mutations.py list | plan | mutmut-plan
Run:     python3 mutations.py run --state /tmp/nsx-math [--case CASE]
Explore: python3 mutations.py mutmut --state /tmp/nsx-math

Only explicit runtime commands create or execute scratch checkouts.
"""

import argparse
import ast
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib


BASE = "989c38952eed869fcd323af64daabd9f705a424d"
ROOT = Path(__file__).resolve().parents[2]
CARD = "custom_components/nightscout_extended/www/nightscout-glucose-card.js"
API = "custom_components/nightscout_extended/api.py"
HISTORY = "custom_components/nightscout_extended/sgv_history.py"
CARD_TESTS = "tests_js/card.test.mjs"
MATH_TEST = "tests_contract/math_review/test_math_contract.py"
PROJECTION_TEST = "tests_contract/math_review/test_projection.py"
STATE_TEST = "tests_contract/math_review/test_ordered_state.py"
HA_CONFIG = "tests_ha/pytest.ini"
MUTMUT_VERSION = "3.8.1"
MUTMUT_PATTERN = "custom_components.nightscout_extended.api.x_select_reservoir__mutmut_*"
MUTMUT_NODE = "test_reservoir_freshness"
MARKER = ".nsx-math-scratch"

PRODUCT_BLOBS = {
    CARD: "abd955aab519a015cd72359b57e0771dc2c0e36a",
    API: "e51578ee02e5bf91f0444628e988b6068c3eefc1",
    HISTORY: "c9a928e73fef81425c4b8bb5ac6f622d71d3d78e",
}


@dataclass(frozen=True)
class Mutation:
    path: str
    before: str
    after: str
    runner: str
    test: str
    node: str
    purpose: str


CATALOGUE = {
    "card-target-equality": Mutation(
        CARD, '    if (mgdl <= t.high) return "in_range";',
        '    if (mgdl < t.high) return "in_range";',
        "node", CARD_TESTS, "classify uses Nightscout thresholds with inclusive target range",
        "Exclude a measurement exactly at the upper target threshold.",
    ),
    "card-round-before-band": Mutation(
        CARD, "    const level = logic.classify(mgdl, ageMin, cfg.stale_minutes, t);",
        "    const level = logic.classify(Math.round(mgdl), ageMin, cfg.stale_minutes, t);",
        "ha", PROJECTION_TEST, "test_projection_classifies_mgdl_at_configured_boundaries[above-180]",
        "Round 180.1 mg/dL to the upper target before selecting the alarm band.",
    ),
    "card-negative-delta": Mutation(
        CARD, '    return (v > 0 ? "+" : "−") + text;',
        '    return "+" + text;',
        "node", CARD_TESTS, "delta formatting in both units",
        "Display negative changes with the positive sign.",
    ),
    "card-mmol-delta-scale": Mutation(
        CARD, "    const v = mmol ? Math.round(delta * 10) / 10 : Math.round(delta);",
        "    const v = mmol ? Math.round(delta / 10) * 10 : Math.round(delta);",
        "node", CARD_TESTS, "delta formatting in both units",
        "Reverse the decimal quantization factors for mmol/L.",
    ),
    "card-double-mmol-to-mgdl": Mutation(
        CARD, "    const mgdl = value * (mmol ? MGDL_PER_MMOL : 1);",
        "    const mgdl = value * (mmol ? MGDL_PER_MMOL * MGDL_PER_MMOL : 1);",
        "ha", PROJECTION_TEST, "test_hypothesis_projection_alarm_band_is_unit_invariant[above-180]",
        "Convert an entered mmol/L state to mg/dL twice for the alarm band and chart.",
    ),
    "ar2-ignore-tolerance": Mutation(
        API, "        return best if abs(best.time.timestamp() - target) <= 180 else None",
        "        return best",
        "pytest", "tests/test_api.py", "test_forecast_too_far_returns_none",
        "Treat a remote forecast point as valid outside the released tolerance.",
    ),
    "ar2-anchor-on-raw-reading": Mutation(
        API, "        return self.glucose_time if self._forecast_anchor is False else self._forecast_anchor",
        "        return self.glucose_time",
        "ha", MATH_TEST, "test_forecast_missing_and_nearest[both]",
        "Replace the preserved AR2 source clock with the later raw glucose reading.",
    ),
    "reservoir-exclusive-cutoff": Mutation(
        API, "        if device is None or contact is None or not earliest <= contact <= now:",
        "        if device is None or contact is None or not earliest < contact <= now:",
        "pytest", "tests/test_reservoir.py", "test_freshness_uses_exact_contact_boundary[3600]",
        "Reject an otherwise valid zero reservoir exactly at the 3,600-second contact boundary.",
    ),
    "reservoir-drop-zero": Mutation(
        API,
        "        observations.setdefault(device, []).append((contact, amount if amount is not None and amount >= 0 else None))",
        "        observations.setdefault(device, []).append((contact, amount if amount is not None and amount > 0 else None))",
        "ha", MATH_TEST, "test_reservoir_quantity[zero-units]",
        "Treat a legitimate 0 U pump reservoir as missing.",
    ),
    "reservoir-use-upload-clock": Mutation(
        API, '        contact = _reservoir_timestamp(status.get("timestamp"))',
        '        contact = _reservoir_timestamp(row.get("created_at"))',
        "ha", MATH_TEST, "test_reservoir_contact_orders_before_upload_clock",
        "Order pump events by their upload time instead of source contact.",
    ),
    "sgv-floor-source-ms": Mutation(
        HISTORY,
        '        return {"id": self.id, "date": self.date, "sgv": self.sgv, "direction": self.direction}',
        '        return {"id": self.id, "date": self.date // 1000 * 1000, '
        '"sgv": self.sgv, "direction": self.direction}',
        "ha", STATE_TEST, "test_exact_quiet_late_and_same_time_history",
        "Collapse a public reading's original millisecond timestamp to seconds.",
    ),
}


class HarnessError(Exception):
    """A fixed, credential-free operational diagnostic."""


def _digest(data):
    return sha256(data).hexdigest()


def _git(root, *args):
    git = shutil.which("git")
    if git is None:
        raise HarnessError("git executable unavailable")
    try:
        result = subprocess.run(
            [git, "-C", str(root), *args], check=False, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=60,
            env={
                "PATH": os.defpath,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError("git read could not complete") from error
    if result.returncode != 0:
        raise HarnessError("git read or clean-check failed")
    return result.stdout


def _checkout(root):
    top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").rstrip(b"\r\n")))
    if top.resolve() != root:
        raise HarnessError("choose a repository top-level directory")
    head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    _git(root, "merge-base", "--is-ancestor", BASE, head)
    _git(root, "diff", "--quiet", head, "--")
    return head


def _tracked(root, head, path):
    location = root / path
    archived = _git(root, "show", f"{head}:{path}")
    if not location.is_file() or not location.resolve().is_relative_to(root):
        raise HarnessError(f"required checkout path is missing: {path}")
    if archived != location.read_bytes():
        raise HarnessError(f"checkout bytes differ from the archived source: {path}")
    return archived


def _selector(test_bytes, runner, node, test):
    source = test_bytes.decode("utf-8")
    if runner == "node":
        return source.count(f"test({json.dumps(node)},")

    base = node.partition("[")[0]
    definitions = [
        item for item in ast.parse(source, filename=test).body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == base
    ]
    if node == base or len(definitions) != 1:
        return len(definitions)

    parameter_id = node.removeprefix(base + "[").removesuffix("]")
    candidates = []
    for decorator in definitions[0].decorator_list:
        if not (
            isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "parametrize" and len(decorator.args) == 2
            and isinstance(decorator.args[1], (ast.List, ast.Tuple))
        ):
            continue
        explicit_ids = next((keyword.value for keyword in decorator.keywords if keyword.arg == "ids"), None)
        if isinstance(explicit_ids, (ast.List, ast.Tuple)):
            candidates.extend(item.value for item in explicit_ids.elts if isinstance(item, ast.Constant))
            continue
        for entry in decorator.args[1].elts:
            if isinstance(entry, ast.Constant):
                candidates.append(str(entry.value))
            elif isinstance(entry, ast.Call) and isinstance(entry.func, ast.Attribute) and entry.func.attr == "param":
                candidates.extend(
                    keyword.value.value for keyword in entry.keywords
                    if keyword.arg == "id" and isinstance(keyword.value, ast.Constant)
                )
    return candidates.count(parameter_id)


def _inspect(root, head, mutation):
    source = _tracked(root, head, mutation.path)
    blob = _git(root, "rev-parse", f"{head}:{mutation.path}").decode("ascii").strip()
    if blob != PRODUCT_BLOBS[mutation.path]:
        raise HarnessError(f"pinned product blob differs: {mutation.path}")
    original = (mutation.before + "\n").encode("utf-8")
    replacement = (mutation.after + "\n").encode("utf-8")
    if source.count(original) != 1 or original == replacement:
        raise HarnessError(f"mutation requires exactly one literal preimage: {mutation.path}")
    test_bytes = _tracked(root, head, mutation.test)
    if _selector(test_bytes, mutation.runner, mutation.node, mutation.test) != 1:
        raise HarnessError(f"focused selector is not unique in {mutation.test}")
    return {
        "before_sha256": _digest(source),
        "mutated_sha256": _digest(source.replace(original, replacement, 1)),
        "blob": blob,
        "preimages": 1,
        "static_selectors": 1,
    }


def _archive(root, head, work):
    tar_bytes = _git(root, "archive", "--format=tar", head)
    with tarfile.open(fileobj=BytesIO(tar_bytes), mode="r:") as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise HarnessError("archive contains a path outside its scratch root")
            target = work.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as stream, target.open("wb") as destination:
                    shutil.copyfileobj(stream, destination)
            else:
                raise HarnessError("archive contains a non-file entry")
    (work / MARKER).touch()


def _tool(state, path):
    tool = state / path
    if not tool.is_file() or not os.access(tool, os.X_OK):
        raise HarnessError(f"staged executable unavailable: {path}")
    # Keep the original venv path. Resolving a python symlink would bypass it.
    return str(tool)


def _run(argv, work, scratch, timeout, *, path=os.defpath):
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
            env={
                "PATH": path,
                "LANG": "C.UTF-8",
                "TMPDIR": str(scratch),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
    except OSError:
        return {"state": "HARNESS_ERROR", "seconds": round(time.monotonic() - started, 3)}
    try:
        return {"returncode": process.wait(timeout=timeout), "seconds": round(time.monotonic() - started, 3)}
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        if isinstance(error, KeyboardInterrupt):
            raise
        return {"state": "TIMEOUT", "seconds": round(time.monotonic() - started, 3)}


def _pytest_child(test, node, receipt):
    scratch_checkout = Path.cwd()
    if not (scratch_checkout / MARKER).is_file() or not Path(__file__).resolve().is_relative_to(scratch_checkout):
        return 4
    sys.path.insert(0, str(scratch_checkout))
    test_path = (scratch_checkout / test).resolve()

    import pytest

    class Results:
        def __init__(self):
            self.collected = []
            self.reports = []
            self.collection_error = False

        def pytest_collection_finish(self, session):
            self.collected = [
                item.path.resolve() == test_path and item.name == node for item in session.items
            ]

        def pytest_collectreport(self, report):
            self.collection_error |= report.failed

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            report = outcome.get_result()
            error = call.excinfo
            assertion = (
                error is not None and error.errisinstance(AssertionError)
                and Path(error.traceback[-1].path).resolve() == test_path
            )
            self.reports.append({
                "selected": item.path.resolve() == test_path and item.name == node,
                "phase": report.when,
                "result": "skipped" if hasattr(report, "wasxfail") else report.outcome,
                "assertion": assertion,
            })

    results = Results()
    rc = pytest.main(
        [f"{test}::{node}", "-c", HA_CONFIG, "-q", "--tb=no", "-p", "no:cacheprovider"],
        plugins=[results],
    )
    Path(receipt).write_text(json.dumps(vars(results)), encoding="utf-8")
    return int(rc)


def _pytest_status(receipt, rc):
    if receipt["collected"] != [True] or receipt["collection_error"]:
        return "HARNESS_ERROR"
    reports = receipt["reports"]
    if not reports or not all(report["selected"] for report in reports):
        return "HARNESS_ERROR"
    phases = {report["phase"]: report for report in reports}
    if len(phases) != len(reports):
        return "HARNESS_ERROR"
    if any(report["phase"] != "call" and report["result"] == "failed" for report in reports):
        return "HARNESS_ERROR"
    if rc == 0 and any(report["result"] == "skipped" for report in reports):
        return "SKIPPED"
    if list(phases) != ["setup", "call", "teardown"] or any(
        phases[phase]["result"] != "passed" for phase in ("setup", "teardown")
    ):
        return "HARNESS_ERROR"
    call = phases["call"]
    if rc == 1 and call["result"] == "failed" and call["assertion"]:
        return "ASSERTION_FAILED"
    if rc == 0 and call["result"] == "passed":
        return "PASSED"
    return "HARNESS_ERROR"


NODE_REPORTER = """
import { writeFileSync } from 'node:fs';
export default async function* reporter(source) {
  const results = { selected: [], unexpected: false };
  for await (const { type, data } of source) {
    if (type !== 'test:pass' && type !== 'test:fail') continue;
    if (data.name !== expected) {
      if (type === 'test:fail' || (!data.skip && !data.todo)) results.unexpected = true;
      continue;
    }
    const error = data.details?.error;
    results.selected.push({
      result: data.skip || data.todo ? 'skipped' : type === 'test:pass' ? 'passed' : 'failed',
      assertion: error?.failureType === 'testCodeFailure' && error.cause?.code === 'ERR_ASSERTION',
    });
  }
  writeFileSync(receipt, JSON.stringify(results), { mode: 0o600 });
}
"""


def _node_status(receipt, rc):
    selected = receipt["selected"]
    if receipt["unexpected"] or len(selected) != 1:
        return "HARNESS_ERROR"
    outcome = selected[0]
    if rc == 0 and outcome["result"] == "skipped":
        return "SKIPPED"
    if rc == 0 and outcome["result"] == "passed":
        return "PASSED"
    if rc == 1 and outcome["result"] == "failed" and outcome["assertion"]:
        return "ASSERTION_FAILED"
    return "HARNESS_ERROR"


def _test(work, scratch, state, mutation, timeout, phase):
    receipt = scratch / f"{phase}.json"
    path = os.defpath
    if mutation.runner == "node":
        reporter = scratch / f"{phase}.mjs"
        reporter.write_text(
            f"const expected = {json.dumps(mutation.node)};\n"
            f"const receipt = {json.dumps(str(receipt))};\n" + NODE_REPORTER,
            encoding="utf-8",
        )
        pattern = re.escape(mutation.node).replace(r"\ ", " ")
        argv = [
            _tool(state, "bin/node"), "--test", "--test-isolation=none",
            f"--test-name-pattern=^{pattern}$", f"--test-reporter={reporter.as_uri()}", mutation.test,
        ]
    else:
        if mutation.test == PROJECTION_TEST:
            node = Path(_tool(state, "bin/node"))
            path = os.pathsep.join((str(node.parent), path))
        argv = [
            _tool(state, "ha/bin/python"), str(work / Path(__file__).relative_to(ROOT)),
            "_pytest", mutation.test, mutation.node, str(receipt),
        ]
    execution = _run(argv, work, scratch, timeout, path=path)
    classify = _node_status if mutation.runner == "node" else _pytest_status
    return _classify_receipt(receipt, execution, classify)


def _classify_receipt(receipt, execution, classify):
    if "state" in execution:
        return execution
    try:
        if receipt.stat().st_size > 4096:
            raise ValueError
        report = json.loads(receipt.read_text(encoding="utf-8"))
        execution["state"] = classify(report, execution["returncode"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        execution["state"] = "HARNESS_ERROR"
    return execution


def _trial(root, head, state, mutation, inspection, timeout):
    with tempfile.TemporaryDirectory(prefix="nsx-math-") as temp:
        scratch = Path(temp)
        checkout = scratch / "checkout"
        checkout.mkdir()
        _archive(root, head, checkout)
        target = checkout / mutation.path
        original = target.read_bytes()
        if _digest(original) != inspection["before_sha256"]:
            raise HarnessError("scratch checkout failed source hash verification")
        baseline = _test(checkout, scratch, state, mutation, timeout, "baseline")
        result = {"baseline": baseline}
        if baseline["state"] != "PASSED":
            status = "FAILED" if baseline["state"] == "ASSERTION_FAILED" else baseline["state"]
            return result | {"state": f"BASELINE_{status}"}

        before = (mutation.before + "\n").encode("utf-8")
        after = (mutation.after + "\n").encode("utf-8")
        mutant = original.replace(before, after, 1)
        try:
            target.write_bytes(mutant)
            if _digest(target.read_bytes()) != inspection["mutated_sha256"]:
                raise HarnessError("applied mutant failed source hash verification")
            result["mutant"] = _test(checkout, scratch, state, mutation, timeout, "mutant")
        finally:
            target.write_bytes(original)
        if _digest(target.read_bytes()) != inspection["before_sha256"]:
            raise HarnessError("restored scratch source differs from the baseline")
        result["restored"] = _test(checkout, scratch, state, mutation, timeout, "restored")
        if result["restored"]["state"] != "PASSED":
            return result | {"state": "RESTORATION_FAILED"}
        status = result["mutant"]["state"]
        return result | {"state": {"PASSED": "SURVIVED", "ASSERTION_FAILED": "KILLED"}.get(status, status)}


def _mutmut_settings():
    return {
        "source_paths": [API],
        "also_copy": ["custom_components", "tests_contract", "tests_ha"],
        "pytest_add_cli_args_test_selection": [f"{MATH_TEST}::{MUTMUT_NODE}"],
        "pytest_add_cli_args": ["-c", HA_CONFIG, "-p", "no:cacheprovider"],
    }


def _inspect_mutmut(root, head):
    blob = _git(root, "rev-parse", f"{head}:{API}").decode("ascii").strip()
    if blob != PRODUCT_BLOBS[API]:
        raise HarnessError("pinned exploratory product file differs")
    _tracked(root, head, API)
    test = _tracked(root, head, MATH_TEST)
    if _selector(test, "ha", MUTMUT_NODE, MATH_TEST) != 1:
        raise HarnessError("exploratory pytest selector is not unique")
    return {
        "state": "PLAN_ONLY",
        "version": MUTMUT_VERSION,
        "argv": ["mutmut", "run", "--max-children", "1", MUTMUT_PATTERN],
        "settings": _mutmut_settings(),
    }


def _explore(root, head, state, timeout):
    with tempfile.TemporaryDirectory(prefix="nsx-math-exploratory-") as temp:
        scratch = Path(temp)
        work = scratch / "checkout"
        work.mkdir()
        _archive(root, head, work)
        mutmut = _tool(state, "mutmut/bin/mutmut")
        interpreter = _tool(state, "mutmut/bin/python")
        version = _run([
            interpreter, "-c",
            "import importlib.metadata as m, sys; sys.exit(int(m.version('mutmut') != sys.argv[1]))",
            MUTMUT_VERSION,
        ], work, scratch, min(timeout, 15))
        if version.get("returncode") != 0:
            return {"state": "EXPLORATORY_ENVIRONMENT_ERROR", "version_check": version}
        # Use the mutmut environment for both this baseline and mutmut's own
        # pytest invocation. The run cannot rely on the separate HA venv.
        receipt = scratch / "exploratory-baseline.json"
        baseline = _run([
            interpreter, str(work / Path(__file__).relative_to(ROOT)), "_pytest",
            MATH_TEST, MUTMUT_NODE, str(receipt),
        ], work, scratch, timeout)
        _classify_receipt(receipt, baseline, _pytest_status)
        if baseline["state"] != "PASSED":
            return {"state": f"EXPLORATORY_BASELINE_{baseline['state']}", "baseline": baseline}

        config_path = work / "pyproject.toml"
        previous = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        if "mutmut" in tomllib.loads(previous).get("tool", {}):
            raise HarnessError("scratch archive already has an exploratory configuration")
        settings = "\n".join(f"{key} = {json.dumps(value)}" for key, value in _mutmut_settings().items())
        config_path.write_text(previous + "\n[tool.mutmut]\n" + settings + "\n", encoding="utf-8")
        run = _run([mutmut, "run", "--max-children", "1", MUTMUT_PATTERN], work, scratch, timeout)
        status = run.get("state") or (
            "EXPLORATORY_COMMAND_COMPLETED" if run["returncode"] == 0 else "EXPLORATORY_COMMAND_FAILED"
        )
        return {"state": status, "baseline": baseline, "command": run}


def main():
    if sys.argv[1:2] == ["_pytest"]:
        return _pytest_child(*sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    plan = commands.add_parser("plan", aliases=["verify"])
    plan.add_argument("--case", choices=CATALOGUE, action="append")
    commands.add_parser("mutmut-plan")
    run = commands.add_parser("run")
    run.add_argument("--case", choices=CATALOGUE, action="append")
    explore = commands.add_parser("mutmut")
    for command, duration in ((run, 90), (explore, 180)):
        command.add_argument("--state", type=Path, required=True)
        command.add_argument("--timeout", type=int, default=duration, choices=range(1, 601), metavar="1..600")
    args = parser.parse_args()
    picks = dict.fromkeys(getattr(args, "case", None) or CATALOGUE)
    catalogue = {
        name: {
            "path": CATALOGUE[name].path, "runner": CATALOGUE[name].runner,
            "selector": f"{CATALOGUE[name].test}::{CATALOGUE[name].node}",
            "purpose": CATALOGUE[name].purpose,
        }
        for name in picks
    }
    if args.command == "list":
        print(json.dumps({"state": "CATALOGUE", "cases": catalogue, "pinned_base": BASE}, indent=2))
        return 0

    try:
        root = args.repo.resolve(strict=True)
        head = _checkout(root)
        if args.command in {"mutmut", "mutmut-plan"}:
            result = _inspect_mutmut(root, head)
            if args.command == "mutmut":
                if os.name != "posix":
                    raise HarnessError("runtime requires the staged Linux environment")
                result |= _explore(root, head, args.state.resolve(strict=True), args.timeout)
                if _checkout(root) != head:
                    raise HarnessError("input checkout moved during the exploratory run")
            print(json.dumps({"head": head, "exploratory": result}, indent=2))
            return 0 if result["state"] in {"PLAN_ONLY", "EXPLORATORY_COMMAND_COMPLETED"} else 2
        inspection = {name: _inspect(root, head, CATALOGUE[name]) for name in picks}
        if args.command != "run":
            print(json.dumps({"state": "PLAN_ONLY", "head": head, "cases": {
                name: catalogue[name] | inspection[name] for name in picks
            }}, indent=2))
            return 0
        if os.name != "posix":
            raise HarnessError("runtime requires the staged Linux environment")
        state = args.state.resolve(strict=True)
        results = {}
        for name in picks:
            results[name] = catalogue[name] | inspection[name] | _trial(
                root, head, state, CATALOGUE[name], inspection[name], args.timeout,
            )
        if _checkout(root) != head:
            raise HarnessError("source checkout moved during runtime")
        print(json.dumps({"head": head, "cases": results}, indent=2))
        if any(result["state"] not in {"KILLED", "SURVIVED"} for result in results.values()):
            return 2
        return 1 if any(result["state"] == "SURVIVED" for result in results.values()) else 0
    except HarnessError as error:
        print(json.dumps({"state": "HARNESS_ERROR", "detail": str(error)}))
        return 2
    except (OSError, ValueError, SyntaxError, TypeError, tarfile.TarError):
        print(json.dumps({"state": "HARNESS_ERROR", "detail": "malformed or unavailable input"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
