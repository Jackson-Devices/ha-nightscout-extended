"""Collect and preview a Nightscout Extended release on two exact local checkouts.

Collect uses the state prepared by tests_contract/harness/bootstrap.py.
Preview requires a disposable local tag on the product checkout. Neither command
creates a tag, contacts Git remotes, installs the integration, or publishes.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


DOMAIN = "nightscout_extended"
PREFIX = f"custom_components/{DOMAIN}/"
HARNESS = "tests_contract/harness"
CONTRACT = "tests_contract/contract"
APPROVED_SUITE = "f1a4e7dddf071f2c6d3eb2b27499ee4536d76ab3"
FROZEN = {
    "service.py": "38f561862f72d66b2eb3130b11c1754d3bf5f2aa6fecfc2dd6c70c596ec0ae74",
    "cases.json": "2c6d28bba1804cb94a4e980b89cf82fed858baac299b3f4a0065d59c2bd73d57",
    "expected.json": "be000d984603f71db40ca06144d0f83ea817a426d3c3ed51f41ebbe9c50ce34d",
    "run.py": "075614f7420abd52615c473f3e4d2f08bf1d047d88569345479c01a4ec0125a5",
    "controls.py": "cb2bb52279d203e0cde1c888eb3034d9ef25588c888c65ebb6d94982315fdc66",
}
REQUIRED_INSTALL = {
    "__init__.py", "manifest.json", "services.yaml", "strings.json",
    "translations/en.json", "www/nightscout-glucose-card.js",
}
PYTEST_GATES = ("api", "ha", "ha-positive", "supplemental")
SUPPLEMENTAL = "tests_contract/adapter/test_supplemental_replay.py"
RAW_REPLAY = f"{SUPPLEMENTAL}::test_supplemental_frozen_replay"
RAW_CASES = {
    b"A-V3-PUBLIC-VERSION", b"A-V3-RENEW", b"A-V3-REVOKED", b"A-V3-SUBJECT-FAILURES",
    b"A-V3-FORBIDDEN", b"G-PUBLIC-DELTA", b"G-V3-DELTA", b"D-CALCULATED", b"D-SETTINGS-ONLY",
    b"F-AR2-MIXED", b"T-LEGACY", b"T-V3-PAGED", b"T-FUTURE-OVERLAP",
}
RAW_FAILURE = re.compile(
    rb"(?m)^FAILED [^\r\n]*::test_supplemental_frozen_replay\[([A-Z0-9-]+)\]"
    rb" - (?:Failed: )?(SUPPLEMENT_MISMATCH|SUPPLEMENT_HARNESS|SUPPLEMENT_POLICY_MISMATCH)\b"
)
CONTROLS = {
    "good": (0, rb"^PASS [1-9]"),
    "wrong-route": (1, rb"^FAIL .*: wire "),
    "wrong-state": (1, rb"^FAIL (?!.*: wire )"),
    "broken-environment": (2, rb"^HARNESS NotADirectoryError:"),
}
SERVER_STAGES = {
    "server-legacy": {"nightscout-replay", "nightscout-client"},
    "server-v3": {"nightscout-v3"},
}
GATES = (*PYTEST_GATES, "card", *(f"control-{name}" for name in CONTROLS), *SERVER_STAGES)
FRESHNESS = timedelta(hours=24)


class Refusal(Exception):
    """Failure whose fixed message may be reported without exposing command output."""


def require(condition, message):
    if not condition:
        raise Refusal(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    result = json.loads(path.read_bytes())
    require(isinstance(result, dict), "Expected a JSON object")
    return result


def save_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def now():
    return datetime.now(timezone.utc)


def environment():
    names = ("PATH", "SystemRoot", "WINDIR", "TMP", "TEMP", "TMPDIR", "HOME", "XDG_RUNTIME_DIR")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
        UV_OFFLINE="1", PIP_NO_INDEX="1", LANG="C.UTF-8",
    )
    return env


def git(repo, *args, required=True):
    proc = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", repo, *args],
        env=environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False,
    )
    require(not required or proc.returncode == 0, "Local Git source check failed")
    return proc.stdout if proc.returncode == 0 else None


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) is not None


def load_candidate(path):
    candidate = read_json(path)
    for field in ("product_sha", "qa_sha", "suite_sha", "upstream_tag_sha"):
        require(sha(candidate.get(field)), f"Invalid candidate {field}")
    require(candidate["product_sha"] != candidate["qa_sha"], "Use separate product and QA commits")
    require(candidate["suite_sha"] == APPROVED_SUITE, "Candidate uses a different approved frozen suite")
    tag = candidate.get("proposed_tag")
    require(isinstance(tag, str) and re.fullmatch(r"v[0-9][0-9A-Za-z._+-]{0,63}", tag), "Invalid proposed tag")
    rows = candidate.get("prs")
    require(isinstance(rows, list) and len(rows) == 6, "Candidate requires six contribution slices")
    heads = set()
    for number, row in enumerate(rows, start=1):
        require(isinstance(row, dict) and row.get("number") == number, "Invalid contribution slice order")
        require(all(sha(row.get(key)) for key in ("head_sha", "base_sha", "first_parent_sha")),
                "Invalid contribution slice commit")
        require(row.get("status") == "qualified" and row.get("upstream_repo") == "aperetti/ha-nightscout-extended",
                "Invalid contribution scope")
        require(all(isinstance(row.get(key), str) and row[key].strip()
                    for key in ("fork_ref", "feature", "upstream_branch", "evidence"))
                and row["fork_ref"].startswith("refs/heads/"), "Incomplete contribution slice evidence")
        heads.add(row["head_sha"])
    require(len(heads) == 6, "Duplicate contribution head")
    return candidate


def exact_checkout(repo, expected, label):
    require(Path(os.fsdecode(git(repo, "rev-parse", "--show-toplevel").strip())).resolve() == repo,
            f"{label} must be a checkout root")
    require(git(repo, "rev-parse", "--verify", "HEAD").strip().decode("ascii") == expected,
            f"{label} checkout has the wrong HEAD")
    require(not git(repo, "status", "--porcelain=v1", "--untracked-files=all"), f"{label} checkout must be clean")


def safe_path(path):
    return (
        isinstance(path, str) and "\\" not in path and ":" not in path
        and all(part not in ("", ".", "..") and part.casefold() != ".git" for part in path.split("/"))
        and all(ord(char) >= 32 for char in path)
    )


def tracked_tree(repo, path):
    rows = git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD", "--", path).split(b"\0")
    entries = {}
    folded = set()
    for row in filter(None, rows):
        info, raw_name = row.split(b"\t", 1)
        mode, kind, object_id = info.decode("ascii").split()
        name = raw_name.decode("utf-8")
        require(safe_path(name) and name.casefold() not in folded, "Unsafe or duplicate tracked path")
        require(kind == "blob" and mode in ("100644", "100755") and sha(object_id),
                "Install or audit tree contains a non-regular entry")
        entries[name] = (mode, object_id)
        folded.add(name.casefold())
    return entries


def tracked_bytes(repo, name, entry):
    path = repo / name
    require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(repo),
            "Required tracked file is missing or unsafe")
    data = path.read_bytes()
    require(data == git(repo, "cat-file", "blob", entry[1]), "Checkout bytes differ from the tracked Git blob")
    return data


def verify_freeze(qa):
    entries = tracked_tree(qa, CONTRACT)
    require(set(entries) == {f"{CONTRACT}/{name}" for name in FROZEN}, "Frozen suite paths differ")
    for name, expected in FROZEN.items():
        path = f"{CONTRACT}/{name}"
        require(digest(tracked_bytes(qa, path, entries[path])) == expected, "Frozen suite content differs")


def check_qa_execution_bytes(qa):
    for root in ("custom_components", "tests", "tests_ha", "tests_js", "tests_contract"):
        paths = tracked_tree(qa, root)
        require(bool(paths), "Required QA source tree is missing")
        for name, entry in paths.items():
            tracked_bytes(qa, name, entry)


def check_install(product, qa):
    entries = tracked_tree(product, PREFIX)
    require(entries == tracked_tree(qa, PREFIX), "Product and QA install trees differ")
    require(all(name.startswith(PREFIX) for name in entries), "Unexpected install root")
    require({PREFIX + name for name in REQUIRED_INSTALL} <= entries.keys(), "Required install files are missing")
    files = {}
    for name, entry in entries.items():
        data = tracked_bytes(product, name, entry)
        require(data == tracked_bytes(qa, name, entry), "Product and QA install bytes differ")
        files[name] = (entry[0], data)
    metadata = tracked_tree(product, "hacs.json")
    require(metadata and metadata == tracked_tree(qa, "hacs.json"), "Product and QA HACS metadata differ")
    hacs_data = tracked_bytes(product, "hacs.json", metadata["hacs.json"])
    require(hacs_data == tracked_bytes(qa, "hacs.json", metadata["hacs.json"]), "HACS metadata bytes differ")
    hacs = json.loads(hacs_data)
    require(isinstance(hacs, dict) and bool(hacs.get("name")) and not hacs.get("zip_release")
            and not hacs.get("content_in_root"), "Unsupported HACS package layout")
    return files


def markdown_section(text, heading, next_heading):
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if heading.fullmatch(line)]
    require(len(starts) == 1, "Required release notes heading is missing or repeated")
    content = []
    for line in lines[starts[0] + 1:]:
        if next_heading.match(line):
            break
        content.append(line)
    result = "\n".join(content).strip()
    require(bool(result), "Required release notes section is empty")
    return result + "\n"


def check_version(candidate, product, install, notes):
    version = candidate["proposed_tag"][1:]
    manifest = json.loads(install[PREFIX + "manifest.json"][1])
    require(isinstance(manifest, dict) and manifest.get("domain") == DOMAIN and manifest.get("version") == version,
            "Proposed tag does not match the integration manifest version")
    card = install[PREFIX + "www/nightscout-glucose-card.js"][1].decode("utf-8")
    versions = re.findall(r"(?m)^\s*(?:export\s+)?const\s+CARD_VERSION\s*=\s*['\"]([^'\"]+)['\"]\s*;", card)
    require(versions == [version], "Proposed tag does not match the card version")
    tag_ref = f"refs/tags/{candidate['proposed_tag']}"
    target = git(product, "rev-parse", "--verify", f"{tag_ref}^{{commit}}", required=False)
    require(target is not None, "Disposable local preview tag is missing")
    require(target.strip().decode("ascii") == candidate["product_sha"], "Local preview tag resolves to the wrong commit")
    changelog = tracked_tree(product, "CHANGELOG.md")
    require("CHANGELOG.md" in changelog, "Tracked changelog is missing")
    changelog_data = tracked_bytes(product, "CHANGELOG.md", changelog["CHANGELOG.md"]).decode("utf-8")
    changelog_notes = markdown_section(
        changelog_data, re.compile(rf"## \[{re.escape(version)}\](?:\s+.*)?"), re.compile(r"^## \["),
    )
    contribution = markdown_section(
        notes.read_text(encoding="utf-8"), re.compile(r"## Proposed contribution release notes\s*"), re.compile(r"^## "),
    )
    return changelog_notes, contribution


def package_install(output, install):
    archive_path = output / f"{DOMAIN}-preview.zip"
    with ZipFile(archive_path, "x", compression=ZIP_DEFLATED) as archive:
        for name, (mode, data) in sorted(install.items()):
            member = ZipInfo(name)
            member.create_system = 3
            member.external_attr = int(mode, 8) << 16
            archive.writestr(member, data, compress_type=ZIP_DEFLATED)
    with TemporaryDirectory(dir=output) as scratch, ZipFile(archive_path) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        require(len(names) == len(set(names)) and set(names) == set(install), "Install archive inventory differs")
        for member in members:
            name = member.filename
            require(safe_path(name) and name.startswith(PREFIX) and not member.is_dir()
                    and stat.S_ISREG(member.external_attr >> 16), "Unsafe install archive entry")
            data = archive.read(member)
            extracted = Path(scratch) / name
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_bytes(data)
            require(extracted.read_bytes() == install[name][1], "Unpacked install bytes differ")
    return {
        "filename": archive_path.name, "sha256": digest(archive_path.read_bytes()),
        "files": [{"path": name, "sha256": digest(data), "size": len(data), "mode": mode}
                  for name, (mode, data) in sorted(install.items())],
    }


def command(repo, argv, env, *, capture_stderr=False):
    try:
        proc = subprocess.run(
            argv, cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL, timeout=420, check=False,
        )
    except subprocess.TimeoutExpired:
        return None, b"", b"", "timeout"
    except OSError:
        return None, b"", b"", "could not launch"
    return proc.returncode, proc.stdout, proc.stderr or b"", None


def pytest_counts(output):
    for line in reversed(output.splitlines()):
        if re.fullmatch(rb"[\w, .=>=<\[\]-]+ in \d+(?:\.\d+)?s(?: \([^)]+\))?", line.strip()):
            return {key.decode("ascii"): int(value) for value, key in re.findall(
                rb"\b(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b", line,
            )}
    return {}


def raw_characterization(exit_code, output, launch_error):
    counts = pytest_counts(output)
    markers = [(case, marker) for case, marker in RAW_FAILURE.findall(output) if case in RAW_CASES]
    mismatches = {case for case, marker in markers if marker == b"SUPPLEMENT_MISMATCH"}
    harness_errors = sum(marker == b"SUPPLEMENT_HARNESS" for _, marker in markers)
    errors = counts.get("error", 0) + counts.get("errors", 0)
    row = {
        "gating": False, "exit_code": exit_code, "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0), "skipped": counts.get("skipped", 0), "errors": errors,
        "counts_observed": bool(counts), "harness_errors": harness_errors,
        "comparison_mismatches": len(mismatches),
        "mismatch_cases": sorted(case.decode("ascii") for case in mismatches),
    }
    if exit_code == 5 or re.search(rb"(?mi)^(?:_+ )?ERROR collecting\b|\b\d+ errors? during collection\b", output):
        row["category"] = "collection_error"
    elif launch_error or exit_code not in (0, 1) or errors or harness_errors:
        row["category"] = "infrastructure_failure"
    elif (exit_code, row["passed"], row["failed"], row["skipped"], len(mismatches), len(markers)) == (1, 1, 12, 0, 12, 12):
        row["category"] = "historical_raw_comparison"
    else:
        row["category"] = "unexpected_raw_result"
    return row


def raw_ok(row):
    if not isinstance(row, dict) or row.get("gating") is not False or row.get("category") != "historical_raw_comparison":
        return False
    expected = {
        "exit_code": 1, "passed": 1, "failed": 12, "skipped": 0,
        "errors": 0, "harness_errors": 0, "comparison_mismatches": 12,
    }
    cases = row.get("mismatch_cases")
    return (
        row.get("counts_observed") is True
        and all(type(row.get(key)) is int and row[key] == value for key, value in expected.items())
        and isinstance(cases, list) and len(cases) == 12 and all(
            isinstance(case, str) and case.encode() in RAW_CASES for case in cases
        ) and len(set(cases)) == 12
    )


def node_counts(output):
    return {key.decode("ascii"): int(value) for key, value in re.findall(
        rb"(?m)^# (tests|pass|fail|cancelled|skipped|todo) (\d+)\r?$", output,
    )}


def native_stages(path, runner, lock_sha):
    if not path.is_file():
        return {}
    data = read_json(path)
    if data.get("runner_id") != runner or data.get("lane") != "server" or data.get("lock_sha256") != lock_sha:
        return {}
    result = {}
    for row in data.get("results", []):
        name = row.get("name", "")
        if name not in {"server-images", "nightscout-replay", "nightscout-client", "nightscout-v3",
                        "server-setup", "server-cleanup"}:
            return {}
        if name in result:
            return {}
        stage_status = row.get("status")
        if stage_status not in {"passed", "product_mismatch", "infrastructure_failure"}:
            return {}
        result[name] = {"passed": stage_status == "passed", "classification": stage_status,
                        "exit_code": row.get("exit_code")}
    return result


def gate_ok(gate, row):
    if not isinstance(row, dict) or not isinstance(row.get("observed"), dict):
        return False
    observed = row["observed"]
    if gate in PYTEST_GATES:
        return row.get("exit_code") == 0 and type(observed.get("passed")) is int and observed["passed"] > 0 and all(
            observed.get(key, 0) == 0 for key in ("failed", "error", "errors", "skipped", "xfailed", "xpassed")
        )
    if gate == "card":
        return (row.get("exit_code") == 0 and type(observed.get("tests")) is int and observed["tests"] > 0
                and observed.get("pass") == observed["tests"] and all(
                    observed.get(key, 0) == 0 for key in ("fail", "cancelled", "skipped", "todo")
                ))
    if gate.startswith("control-"):
        expected_exit = CONTROLS[gate.removeprefix("control-")][0]
        stream = "stdout" if expected_exit == 0 else "stderr"
        return (row.get("exit_code") == expected_exit and observed.get("marker") is True
                and observed.get("stream") == stream)
    return (row.get("exit_code") == 0 and SERVER_STAGES[gate] <= observed.keys()
            and all(isinstance(value, dict) and value.get("passed") is True for value in observed.values())
            and all(observed[name].get("exit_code") == 0
                    for name in SERVER_STAGES[gate] if name.startswith("nightscout-client") or name == "nightscout-v3"))


def collect(args, candidate, output):
    qa = args.qa.resolve()
    state = args.state.resolve()
    require(re.fullmatch(r"[0-9A-Za-z._-]{1,64}", args.runner_id), "Invalid runner identifier")
    require(not state.is_relative_to(qa), "Staged dependencies must be outside the QA checkout")
    exact_checkout(qa, candidate["qa_sha"], "QA")
    verify_freeze(qa)
    check_qa_execution_bytes(qa)
    lock = (qa / HARNESS / "versions.lock").read_bytes()
    lock_sha = digest(lock)
    require(read_json(state / "ready.json").get("lock_sha256") == lock_sha, "Stage the exact QA dependency lock first")
    specs = json.loads(lock)
    parser = state / "parser/bin/python"
    ha = state / "ha/bin/python"
    node = state / "bin/node"
    env = environment()
    for binary, expected in ((parser, "Python " + specs["environments"]["parser"]["python"]),
                             (ha, "Python " + specs["environments"]["ha"]["python"]),
                             (node, "v" + specs["node"]["version"])):
        code, captured, _, _ = command(qa, [binary, "--version"], env)
        require(code == 0 and captured.strip() == expected.encode(), "Staged runtime version differs from QA lock")

    py = ["-m", "pytest", "-q", "--color=no", "--tb=no", "--show-capture=no", "-p", "no:cacheprovider"]
    config = ["-c", "tests_ha/pytest.ini"]
    tests = {
        "api": [parser, *py, "tests"],
        "ha": [ha, *py, *config, "tests_ha"],
        "ha-positive": [ha, *py, *config, "tests_contract/adapter/test_upstream_checks.py::test_upstream_setup_and_lifecycle",
                        "tests_contract/adapter/test_forecast_compatibility.py"],
        "supplemental": [ha, *py, *config, f"{SUPPLEMENTAL}::test_supplemental_integrated_compatibility",
                         f"{SUPPLEMENTAL}::test_supplemental_late_calendar_fault"],
        "card": [node, "--test", "--test-reporter=tap",
                 *sorted(path for path in tracked_tree(qa, "tests_js") if path.endswith(".test.mjs"))],
    }
    require(len(tests["card"]) > 3, "QA card test files are missing")
    tests.update({f"control-{name}": [parser, f"{CONTRACT}/run.py", name] for name in CONTROLS})
    started = now()
    records = {}
    with TemporaryDirectory(dir=output) as scratch:
        private = Path(scratch)
        docker_config = private / "docker"
        docker_config.mkdir()
        env["DOCKER_CONFIG"] = str(docker_config)
        for gate in GATES:
            exact_checkout(qa, candidate["qa_sha"], "QA")
            runner = f"{args.runner_id}-{gate}"
            if gate in SERVER_STAGES:
                native_output = private / gate
                argv = [parser, f"{HARNESS}/run.py", "server", "--state", state,
                        "--runner-id", runner, "--output", native_output]
                if gate == "server-v3":
                    argv.append("--v3")
                elif args.pull:
                    argv.append("--pull")
            else:
                argv = tests[gate]
            is_control = gate.startswith("control-")
            exit_code, captured, stderr, error = command(qa, argv, env, capture_stderr=is_control)
            if gate in PYTEST_GATES:
                observed = pytest_counts(captured)
            elif gate == "card":
                observed = node_counts(captured)
            elif is_control:
                expected_exit, pattern = CONTROLS[gate.removeprefix("control-")]
                stream = "stdout" if expected_exit == 0 else "stderr"
                source = captured if stream == "stdout" else stderr
                observed = {"stream": stream, "marker": re.search(pattern, source, re.MULTILINE) is not None}
            else:
                observed = native_stages(native_output / "results.json", runner, lock_sha)
            row = {"gate": gate, "qa_sha": candidate["qa_sha"], "exit_code": exit_code, "observed": observed}
            if error:
                row["diagnostic"] = error
            row["status"] = "passed" if gate_ok(gate, row) else "failed"
            records[gate] = row
            exact_checkout(qa, candidate["qa_sha"], "QA")
            print(f"{gate} [{row['status']}] exit {exit_code}", flush=True)
        raw_py = ["--tb=line" if option == "--tb=no" else option for option in py]
        raw_exit, raw_output, _, raw_error = command(
            qa, [ha, *raw_py, "-rfE", *config, RAW_REPLAY], {**env, "COLUMNS": "240"},
        )
        raw = raw_characterization(raw_exit, raw_output, raw_error)
        print(f"raw supplemental [{raw['category']}] exit {raw_exit}: "
              f"{raw['passed']} passed, {raw['failed']} failed, {raw['skipped']} skipped", flush=True)
        exact_checkout(qa, candidate["qa_sha"], "QA")
    check_qa_execution_bytes(qa)
    evidence = {
        "schema": 1, "product_sha": candidate["product_sha"], "qa_sha": candidate["qa_sha"],
        "suite_sha": candidate["suite_sha"], "script_sha256": digest(Path(__file__).read_bytes()),
        "lock_sha256": lock_sha, "runner_id": args.runner_id, "started_at": started.isoformat(),
        "runtimes": {"parser": specs["environments"]["parser"]["python"],
                     "home_assistant_python": specs["environments"]["ha"]["python"], "node": specs["node"]["version"]},
        "finished_at": now().isoformat(), "gates": records, "raw_characterization": raw,
    }
    save_json(output / "gates.json", evidence)
    return 0 if raw_ok(raw) and all(row["status"] == "passed" for row in records.values()) else 1


def verify_evidence(candidate, qa, path):
    supplied = path.read_bytes()
    evidence = json.loads(supplied)
    require(isinstance(evidence, dict), "Expected a gate evidence object")
    require(evidence.get("schema") == 1 and all(evidence.get(key) == candidate[key]
            for key in ("product_sha", "qa_sha", "suite_sha")), "Gate evidence belongs to a different source")
    require(evidence.get("script_sha256") == digest(Path(__file__).read_bytes()), "Gate collector source differs")
    require(evidence.get("lock_sha256") == digest((qa / HARNESS / "versions.lock").read_bytes()),
            "Gate evidence uses a different dependency lock")
    runner = evidence.get("runner_id")
    require(isinstance(runner, str) and re.fullmatch(r"[0-9A-Za-z._-]{1,64}", runner), "Invalid evidence runner")
    started = datetime.fromisoformat(evidence["started_at"])
    finished = datetime.fromisoformat(evidence["finished_at"])
    current = now()
    require(current - FRESHNESS <= started <= finished <= current, "Gate evidence is outside the fresh window")
    gates = evidence.get("gates")
    require(isinstance(gates, dict), "Required gate results are missing")
    for gate in GATES:
        require(gate in gates, f"Required gate result is missing: {gate}")
        row = gates[gate]
        require(isinstance(row, dict) and row.get("gate") == gate and row.get("qa_sha") == candidate["qa_sha"]
                and row.get("status") == "passed" and gate_ok(gate, row), f"Required gate failed: {gate}")
    require(set(gates) == set(GATES), "Gate evidence has unexpected entries")
    raw = evidence.get("raw_characterization")
    require(raw_ok(raw), "Historical raw comparison is missing or unexpectedly changed")
    safe_raw = {key: raw[key] for key in (
        "gating", "category", "exit_code", "passed", "failed", "skipped", "errors",
        "counts_observed", "harness_errors", "comparison_mismatches", "mismatch_cases",
    )}
    accepted = {gate: {"status": "passed", "exit_code": gates[gate]["exit_code"]} for gate in GATES}
    return runner, accepted, safe_raw, digest(supplied)


def preview(args, candidate, output):
    qa, product = args.qa.resolve(), args.product.resolve()
    require(qa != product and not output.is_relative_to(product), "Use separate exact checkouts and external output")
    exact_checkout(product, candidate["product_sha"], "Product")
    exact_checkout(qa, candidate["qa_sha"], "QA")
    verify_freeze(qa)
    check_qa_execution_bytes(qa)
    install = check_install(product, qa)
    notes = args.notes or args.candidate.with_name("daybreak.md")
    changelog, contribution = check_version(candidate, product, install, notes)
    runner, gates, raw, evidence_sha = verify_evidence(candidate, qa, args.evidence)
    package = package_install(output, install)
    (output / "changelog-section.md").write_text(changelog, encoding="utf-8")
    (output / "contribution-notes.md").write_text(contribution, encoding="utf-8")
    save_json(output / "receipt.json", {
        "schema": 1, "status": "preview_passed", "product_sha": candidate["product_sha"],
        "qa_sha": candidate["qa_sha"], "suite_sha": candidate["suite_sha"], "preview_tag": candidate["proposed_tag"],
        "local_tag_target": candidate["product_sha"], "upstream_tag_recorded_target": candidate["upstream_tag_sha"],
        "upstream_tag_matches_candidate": candidate["upstream_tag_sha"] == candidate["product_sha"],
        "publication_status": "hold", "gate_evidence_sha256": evidence_sha,
        "runner_id": runner, "gates": gates, "raw_characterization": raw,
        "changelog_sha256": digest(changelog.encode()), "contribution_notes_sha256": digest(contribution.encode()),
        "package": package, "completed_at": now().isoformat(),
    })
    print("Preview passed. Publication status is hold.", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    lanes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("collect", "preview"):
        lane = lanes.add_parser(mode)
        lane.add_argument("--candidate", type=Path, required=True)
        lane.add_argument("--qa", type=Path, required=True)
        lane.add_argument("--output", type=Path, required=True, help="Fresh directory outside all input checkouts")
        if mode == "collect":
            lane.add_argument("--state", type=Path, required=True)
            lane.add_argument("--runner-id", required=True)
            lane.add_argument("--pull", action="store_true", help="Pull the pinned public Docker images for collection")
        else:
            lane.add_argument("--product", type=Path, required=True)
            lane.add_argument("--evidence", type=Path, required=True)
            lane.add_argument("--notes", type=Path, help="Defaults to daybreak.md next to candidate.json")
    args = parser.parse_args()
    output = args.output.resolve()
    created = False
    try:
        input_roots = [args.qa.resolve()]
        input_roots.append(args.state.resolve() if args.mode == "collect" else args.product.resolve())
        require(all(not output.is_relative_to(root) for root in input_roots),
                "Use an output directory outside the input checkouts and staged state")
        output.mkdir(parents=True, exist_ok=False)
        created = True
        candidate = load_candidate(args.candidate)
        return collect(args, candidate, output) if args.mode == "collect" else preview(args, candidate, output)
    except Refusal as exc:
        diagnostic = str(exc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired) as exc:
        diagnostic = f"Could not read or validate required inputs ({type(exc).__name__})"
    if created:
        save_json(output / "receipt.json", {"schema": 1, "status": "refused", "reason": diagnostic})
    print(diagnostic, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
