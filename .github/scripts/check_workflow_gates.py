"""Check the repository's Actions entry points and mandatory preflight edges."""

import re
import sys
from pathlib import Path

import yaml


BOOL_TAG = "tag:yaml.org,2002:bool"
PIN = re.compile(r"[^@]+@[0-9a-f]{40}")
LINT = "./.github/workflows/workflow-lint.yml"
SCAN = "./.github/workflows/codeql-scan.yml"
PARITY = "./.github/workflows/parity.yml"
SUCCESS = "needs.preflight.result == 'success'"
READY = "(github.event_name != 'pull_request' || (!github.event.pull_request.draft && github.event.action != 'converted_to_draft'))"
OPT_IN = "vars.CODEQL_ADVANCED_ENABLED == 'true'"
DOC_PATHS = ["docs/**", "README.md", "CHANGELOG.md", "SECURITY.md"]
READ = {"contents": "read"}
SECURITY_WRITE = {**READ, "security-events": "write"}
KNOWN_VALIDATE_JOBS = {"preflight", "hassfest", "hacs", "tests", "tests-card", "tests-ha", "codeql"}


class WorkflowLoader(yaml.SafeLoader):
    # Actions treats "on" as a key, not as a YAML 1.1 boolean.
    yaml_implicit_resolvers = {
        first: [rule for rule in rules if rule[0] != BOOL_TAG]
        for first, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key, value in self.construct_pairs(node, deep=deep):
            if key in result:
                raise yaml.MarkedYAMLError(problem="duplicate key", problem_mark=node.start_mark)
            result[key] = value
        return result


WorkflowLoader.add_implicit_resolver(BOOL_TAG, re.compile(r"^(?:true|false)$", re.I), list("tTfF"))


def expression(text):
    return "$" + "{{ " + text + " }}"


def checked_condition(actual, expected):
    return isinstance(actual, str) and actual.split() == expression(expected).split()


def caller(filename):
    return f"startsWith(github.workflow_ref, format('{{0}}/.github/workflows/{filename}@', github.repository))"


def check_lint_job(job, error):
    if set(job) != {"runs-on", "timeout-minutes", "steps"} or job.get("runs-on") != "ubuntu-24.04":
        error("preflight runner must be the unconditional hosted Ubuntu lint job")
    timeout = job.get("timeout-minutes")
    if type(timeout) is not int or not 1 <= timeout <= 5:
        error("preflight timeout must be 1–5 minutes")
    steps = job.get("steps")
    step_keys = [
        "actions/checkout", "actions/setup-python", "Check workflow gates",
        "actions/setup-node", "Check Python and card syntax", "Lint workflows and changelog script",
    ]
    if not isinstance(steps, list) or len(steps) != len(step_keys):
        error("preflight must contain only its checkout, setup and lint steps")
        return
    for step, key in zip(steps, step_keys):
        if not isinstance(step, dict):
            error("malformed preflight step")
            continue
        if "if" in step or "working-directory" in step or "env" in step:
            error("preflight steps must run unconditionally in the checked-out tree")
        if key.startswith("actions/"):
            reference = step.get("uses", "")
            if not isinstance(reference, str) or not reference.startswith(key + "@") or not PIN.fullmatch(reference):
                error(f"{key} must use a pinned action")
        elif step.get("name") != key or step.get("shell") != "bash" or not isinstance(step.get("run"), str):
            error(f"{key} must be an unconditional Bash step")
    if isinstance(steps[0], dict) and steps[0].get("with") != {"persist-credentials": False}:
        error("preflight must check out the workflow commit without persistent credentials")
    gate_step = steps[2]
    gate_run = gate_step.get("run") if isinstance(gate_step, dict) else None
    if not isinstance(gate_run, str) or gate_run.splitlines() != [
        "python -m pip --disable-pip-version-check install --only-binary :all: --require-hashes "
        "-r .github/scripts/requirements-preflight.txt",
        "python .github/scripts/check_workflow_gates.py",
        "python .github/scripts/test_workflow_gates.py",
    ]:
        error("preflight must execute the repository workflow gate checker and its controls")


def check_entry_jobs(name, jobs, has_parity, error):
    if jobs.get("preflight") != {"uses": LINT}:
        error("preflight must be an unconditional same-commit call to workflow-lint.yml")
    required = {
        "validate.yml": KNOWN_VALIDATE_JOBS | ({"parity"} if has_parity else set()),
        "codeql.yml": {"preflight", "scan"},
        "release.yml": {"preflight", "release"},
    }[name]
    if required - jobs.keys():
        error(f"required jobs missing: {', '.join(sorted(required - jobs.keys()))}")
    if name == "validate.yml" and has_parity != ("parity" in jobs):
        error("stock parity caller and reusable workflow must be present together")

    for job_id, job in jobs.items():
        if job_id == "preflight":
            continue
        if job.get("needs") not in ("preflight", ["preflight"]):
            error(f"{job_id} must directly need preflight")
        condition = SUCCESS + (f" && {READY}" if name == "validate.yml" else "")
        is_scan = (name, job_id) in {("validate.yml", "codeql"), ("codeql.yml", "scan")}
        if is_scan:
            condition += f" && {OPT_IN}"
        if not checked_condition(job.get("if"), condition):
            error(f"{job_id} has an unapproved gate condition")
        target = SCAN if is_scan else PARITY if (name, job_id) == ("validate.yml", "parity") and has_parity else None
        if job.get("uses") != target:
            error(f"{job_id} has an unapproved reusable workflow target")
        if "with" in job or "secrets" in job:
            error(f"{job_id} must not take caller inputs or secrets")
        expected_permissions = SECURITY_WRITE if is_scan else {"contents": "write"} if (name, job_id) == ("release.yml", "release") else None
        if job.get("permissions") != expected_permissions:
            error(f"{job_id} changes its approved permissions")


def validate(root):
    workflow_dir = root / ".github/workflows"
    if workflow_dir.is_symlink() or not workflow_dir.is_dir():
        return ["missing or symbolic .github/workflows directory"]
    paths = {
        path.name: path for path in workflow_dir.iterdir()
        if path.suffix.lower() in {".yaml", ".yml"}
    }
    errors = []
    triggers = {
        "validate.yml": {
            "push": {"branches": ["main"], "paths-ignore": DOC_PATHS},
            "pull_request": {
                "types": ["opened", "synchronize", "reopened", "ready_for_review", "converted_to_draft"],
                "paths-ignore": DOC_PATHS,
            },
            "schedule": [{"cron": "0 6 * * 1"}],
            "workflow_dispatch": None,
        },
        "workflow-lint.yml": {"workflow_call": None, "workflow_dispatch": None},
        "codeql.yml": {"workflow_dispatch": None},
        "codeql-scan.yml": {"workflow_call": None},
        "release.yml": {"push": {"tags": ["v*"]}},
    }
    has_parity = "parity.yml" in paths
    if has_parity:
        triggers["parity.yml"] = {"workflow_call": None}
    errors.extend(f"{name}: missing required workflow" for name in sorted(triggers.keys() - paths.keys()))

    for name, path in sorted(paths.items()):
        def error(message):
            errors.append(f"{name}: {message}")

        if name not in triggers:
            error("unexpected workflow; review and declare its preflight topology")
            continue
        if path.is_symlink() or not path.is_file():
            error("workflow must be a regular file")
            continue
        try:
            workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=WorkflowLoader)
        except (UnicodeError, OSError, TypeError, yaml.YAMLError) as exc:
            mark = getattr(exc, "problem_mark", None)
            error("invalid YAML or duplicate key" + (f" near line {mark.line + 1}" if mark else ""))
            continue
        if not isinstance(workflow, dict) or workflow.keys() - {"name", "on", "permissions", "concurrency", "jobs"}:
            error("unapproved workflow structure")
            continue
        if workflow.get("on") != triggers[name]:
            error("unapproved event configuration")
        if workflow.get("permissions") != ({} if name == "codeql-scan.yml" else READ):
            error("unapproved workflow permissions")
        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict) or not jobs or any(not isinstance(job, dict) for job in jobs.values()):
            error("malformed jobs")
            continue
        for job_id, job in jobs.items():
            steps = job.get("steps", [])
            if not isinstance(steps, list):
                error(f"{job_id} has malformed steps")
                continue
            if "continue-on-error" in job or any(
                isinstance(step, dict) and "continue-on-error" in step for step in steps
            ):
                error(f"{job_id} cannot use continue-on-error")
        if name in {"validate.yml", "release.yml", "codeql.yml"}:
            check_entry_jobs(name, jobs, has_parity, error)
            continue
        if name == "workflow-lint.yml":
            if jobs.keys() != {"workflow-lint"}:
                error("preflight must contain one lint job")
            else:
                check_lint_job(jobs["workflow-lint"], error)
            continue

        # These workflow_call-only leaves inherit the preflight edge in each
        # caller and can run only in the explicitly reviewed repository paths.
        job_id, filenames, permissions = (
            ("analyze", ["validate.yml", "codeql.yml"], SECURITY_WRITE) if name == "codeql-scan.yml"
            else ("stock", ["validate.yml"], None)
        )
        if jobs.keys() != {job_id}:
            error("reusable workflow must contain only the reviewed job")
            continue
        job = jobs[job_id]
        if job.get("permissions") != permissions or any(key in job for key in ("uses", "needs", "secrets")):
            error(f"{job_id} has unapproved permissions or another workflow edge")
        if not checked_condition(job.get("if"), " || ".join(map(caller, filenames))):
            error(f"{job_id} has an unapproved caller condition")
    return errors


if __name__ == "__main__":
    errors = validate(Path.cwd())
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        sys.exit(1)
    print("Workflow preflight topology passed.")
