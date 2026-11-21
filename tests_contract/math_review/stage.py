"""Stage or check the isolated Linux Nightscout math-review environments.

    python3 tests_contract/math_review/stage.py --state /tmp/nsx-math
    python3 tests_contract/math_review/stage.py --state /tmp/nsx-math --check

Use ha/bin/python for Home Assistant pytest and bin/node for the card. Use
mutmut/bin/mutmut from a disposable working copy for exploratory mutation work.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from tempfile import NamedTemporaryFile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HARNESS = HERE.parent / "harness"
OWNER = {"schema": 1, "owner": "nightscout_extended_math_review"}
TOOL_PATHS = {
    "ha_python": "ha/bin/python",
    "mutmut_python": "mutmut/bin/python",
    "mutmut": "mutmut/bin/mutmut",
    "node": "bin/node",
    "uv": "bin/uv",
}
PROBE = """
from importlib import import_module
from importlib.metadata import distributions
import json
import sys

for module in sys.argv[1:]:
    import_module(module)
print(json.dumps({dist.metadata["Name"]: dist.version for dist in distributions()}))
"""


class StageError(Exception):
    """A stage failure whose message contains no subprocess output."""


def require(condition, message):
    if not condition:
        raise StageError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_object(path):
    data = json.loads(path.read_bytes())
    require(isinstance(data, dict), "Unexpected staging metadata format")
    return data


def write_object(path, value):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except (OSError, ValueError, TypeError):
            staged.unlink(missing_ok=True)
            raise
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def expected_packages(requirements):
    packages = {}
    for requirement in requirements:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==(\S+)(?: --hash=sha256:[a-f0-9]{64})+", requirement)
        require(match is not None, "An environment dependency lacks an exact hashed lock")
        name, version = match.groups()
        name = normalize(name)
        require(name not in packages, "The overlay duplicates a locked base package")
        packages[name] = version
    return packages


def inputs():
    base_bytes = (HARNESS / "versions.lock").read_bytes()
    overlay_bytes = (HERE / "math.lock").read_bytes()
    base, overlay = json.loads(base_bytes), json.loads(overlay_bytes)
    require(overlay.get("schema") == 1 and overlay.get("base_lock_sha256") == sha256(base_bytes),
            "The math overlay does not match the immutable QA base lock")
    ha = [*base["environments"]["ha"]["requirements"], *overlay["ha_additions"]]
    environments = {"ha": ha, "mutmut": [*ha, *overlay["mutmut_additions"]]}
    return base, overlay, environments, sha256(overlay_bytes)


def safe_environment(state):
    names = ("PATH", "HOME", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "LANG", "LC_ALL")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(
        UV_CACHE_DIR=str(state / "uv-cache"),
        UV_PYTHON_INSTALL_DIR=str(state / "python"),
        UV_BUILD_CONSTRAINT=str(state / "build-constraints.txt"),
        UV_NO_PROGRESS="1",
        UV_KEYRING_PROVIDER="disabled",
        NETRC=os.devnull,
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def command(state, env, phase, *args):
    try:
        result = subprocess.run(
            args, cwd=state, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=600, check=False,
        )
    except subprocess.TimeoutExpired:
        raise StageError(f"{phase} timed out") from None
    except OSError:
        raise StageError(f"{phase} could not start") from None
    require(result.returncode == 0, f"{phase} failed with exit {result.returncode}")
    return result.stdout


def inspect(state, base, overlay, environments, overlay_sha):
    env = safe_environment(state)
    python_version = base["environments"]["ha"]["python"]
    uv_output = command(state, env, "uv version", state / TOOL_PATHS["uv"], "--version").split()
    require(uv_output[:2] == [b"uv", base["uv"]["version"].encode()], "Staged uv version differs")
    node_output = command(state, env, "Node version", state / TOOL_PATHS["node"], "--version").strip()
    require(node_output == ("v" + base["node"]["version"]).encode(), "Staged Node version differs")
    require(os.access(state / TOOL_PATHS["mutmut"], os.X_OK), "Staged mutmut executable is missing")
    versions = {"python": python_version, "node": base["node"]["version"], "uv": base["uv"]["version"]}
    counts = {}
    for name, requirements in environments.items():
        interpreter = state / name / "bin" / "python"
        output = command(state, env, f"{name} Python version", interpreter, "--version")
        require(output.strip() == f"Python {python_version}".encode(), f"{name} Python version differs")
        modules = ["homeassistant", "pytest", "pytest_homeassistant_custom_component", "freezegun",
                   "hypothesis", "sqlite3", "zoneinfo"]
        if name == "mutmut":
            modules.extend(("mutmut", "libcst", "setproctitle"))
        packages = json.loads(command(state, env, f"{name} imports", interpreter, "-I", "-B", "-c", PROBE, *modules))
        actual = {normalize(key): value for key, value in packages.items()}
        expected = expected_packages(requirements)
        require(actual == expected, f"{name} installed packages differ from the locks")
        counts[name] = len(actual)
        for key in ("homeassistant", "pytest", "hypothesis", "mutmut"):
            if key in actual:
                versions[key] = actual[key]
    return {
        "schema": 1, "base_lock_sha256": overlay["base_lock_sha256"], "math_lock_sha256": overlay_sha,
        "tools": TOOL_PATHS, "versions": versions, "package_counts": counts,
    }


def stage(state, base, environments):
    sys.path.insert(0, str(HARNESS))
    from bootstrap import stage_binary

    uv = stage_binary(state, "uv", base["uv"])
    stage_binary(state, "node", base["node"])
    env = safe_environment(state)
    (state / "build-constraints.txt").write_text("\n".join(base["build_requirements"]) + "\n", encoding="utf-8")

    def call(phase, *args):
        return command(state, env, phase, uv, "--no-config", *args)

    python_version = base["environments"]["ha"]["python"]
    call("managed Python installation", "python", "install", python_version)
    for name, requirements in environments.items():
        expected_packages(requirements)
        path = state / name
        interpreter = path / "bin" / "python"
        requirement_path = state / f"{name}-requirements.txt"
        requirement_path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        if not interpreter.is_file():
            call(f"{name} venv", "venv", "--managed-python", "--python", python_version, path)
        version = command(state, env, f"{name} Python version", interpreter, "--version")
        require(version.strip() == f"Python {python_version}".encode(), f"{name} Python version differs")
        call(f"{name} dependency sync", "pip", "sync", "--python", interpreter, "--require-hashes", "--strict",
             "--default-index", base["index"], requirement_path)
        call(f"{name} dependency check", "pip", "check", "--python", interpreter)


def execute(state, check_only):
    require(platform.system() == "Linux" and platform.machine() == "x86_64",
            "Staging targets Linux x86_64 with glibc")
    require(not state.is_relative_to(ROOT), "Use a staging directory outside the source checkout")
    import fcntl

    base, overlay, environments, overlay_sha = inputs()
    for requirements in environments.values():
        expected_packages(requirements)
    owner = state / "math-state.json"
    ready = state / "math-ready.json"
    lock = state / ".stage.lock"
    if not check_only:
        if not owner.exists() and state.exists():
            require(all(item == lock for item in state.iterdir()), "Staging directory belongs to another workflow")
        state.mkdir(parents=True, exist_ok=True)
    else:
        require(owner.is_file() and lock.is_file(), "The math environments have not been staged")
    with lock.open("rb" if check_only else "a+b") as held:
        fcntl.flock(held, fcntl.LOCK_SH if check_only else fcntl.LOCK_EX)
        if not owner.exists():
            require(not check_only and all(item == lock for item in state.iterdir()),
                    "Staging directory belongs to another workflow")
            write_object(owner, OWNER)
        require(read_object(owner) == OWNER, "Staging directory belongs to another workflow")
        if ready.is_file():
            prior = read_object(ready)
            if prior.get("math_lock_sha256") == overlay_sha:
                try:
                    current = inspect(state, base, overlay, environments, overlay_sha)
                except (StageError, OSError, ValueError):
                    if check_only:
                        raise
                else:
                    require(prior == current, "Existing staging receipt differs from the checked environment")
                    return current
        require(not check_only, "The staging receipt is missing or uses a different lock")
        ready.unlink(missing_ok=True)
        stage(state, base, environments)
        receipt = inspect(state, base, overlay, environments, overlay_sha)
        write_object(ready, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", required=True, type=Path, help="Dedicated staging directory outside the checkout")
    parser.add_argument("--check", action="store_true", help="Check staged imports and versions without fetching or writing")
    args = parser.parse_args()
    try:
        receipt = execute(args.state.resolve(), args.check)
    except StageError as exc:
        parser.exit(2, f"Math staging failed: {exc}\n")
    except (OSError, ValueError, KeyError, AttributeError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"Math staging failed ({type(exc).__name__})\n")
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
