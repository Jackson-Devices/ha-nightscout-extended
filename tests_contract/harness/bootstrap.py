"""Stage the offline Nightscout test environments on Linux x86_64.

    python3 tests_contract/harness/bootstrap.py --state /tmp/nightscout-h2

Run from an approved checkout with access to the pinned public release sources.
The state directory holds executables and is private to this checkout and user.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen

HARNESS = Path(__file__).resolve().parent
LOCK = HARNESS / "versions.lock"


def read_lock():
    data = LOCK.read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def clean_env():
    return {
        key: value for key, value in os.environ.items()
        if not key.startswith(("UV_", "PIP_", "PYTHON", "PYTEST_"))
    }


def stage_binary(state, name, spec):
    target = state / "bin" / name
    if target.is_file():
        version = subprocess.check_output([target, "--version"], text=True).split()
        expected = f"v{spec['version']}" if name == "node" else spec["version"]
        if expected in version:
            return target
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=state) as scratch:
        archive = Path(scratch) / "archive"
        with urlopen(spec["url"], timeout=60) as response, archive.open("wb") as destination:
            shutil.copyfileobj(response, destination)
        with archive.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != spec["sha256"]:
                raise ValueError(f"Checksum mismatch for {name} {spec['version']}")
        executable = Path(scratch) / name
        with tarfile.open(archive) as compressed:
            member = compressed.getmember(spec["member"])
            if not member.isfile():
                raise ValueError(f"Unexpected {name} archive member type")
            with compressed.extractfile(member) as source, executable.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        executable.chmod(0o755)
        executable.replace(target)
    return target


def bootstrap(state):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("The harness lock targets Linux x86_64 with glibc")
    lock, lock_sha256 = read_lock()
    state.mkdir(parents=True, exist_ok=True)
    ready = state / "ready.json"
    ready.unlink(missing_ok=True)
    uv = stage_binary(state, "uv", lock["uv"])
    stage_binary(state, "node", lock["node"])
    env = clean_env()
    env["UV_CACHE_DIR"] = str(state / "uv-cache")
    env["UV_PYTHON_INSTALL_DIR"] = str(state / "python")
    env["UV_NO_PROGRESS"] = "1"
    env["UV_BUILD_CONSTRAINT"] = str(state / "build-constraints.txt")
    (state / "build-constraints.txt").write_text(
        "\n".join(lock["build_requirements"]) + "\n", encoding="utf-8"
    )

    def call(*args):
        subprocess.run([uv, "--no-config", *args], env=env, check=True)

    call("python", "install", *(spec["python"] for spec in lock["environments"].values()))
    for name, spec in lock["environments"].items():
        interpreter = state / name / "bin" / "python"
        requirements = state / f"{name}-requirements.txt"
        requirements.write_text("\n".join(spec["requirements"]) + "\n", encoding="utf-8")
        if not interpreter.is_file():
            call("venv", "--managed-python", "--python", spec["python"], str(state / name))
        version = subprocess.check_output([interpreter, "--version"], env=env, text=True).strip()
        if version != f"Python {spec['python']}":
            raise ValueError(f"Unexpected interpreter in {state / name}: {version}")
        call(
            "pip", "sync", "--python", str(interpreter), "--require-hashes", "--strict",
            "--default-index", lock["index"], str(requirements),
        )
        call("pip", "check", "--python", str(interpreter))

    with tempfile.NamedTemporaryFile(mode="w", dir=state, encoding="utf-8", delete=False) as receipt:
        json.dump({"lock_sha256": lock_sha256}, receipt)
    Path(receipt.name).replace(ready)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    try:
        bootstrap(args.state.resolve())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"Setup failed: {exc}\n")


if __name__ == "__main__":
    main()
