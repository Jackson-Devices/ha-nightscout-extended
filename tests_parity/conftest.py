"""Shared fixtures for the parity tests.

These tests run the integration against a real, pinned stock Nightscout started by ``stock.sh``. Expected
values never come from this repository: they come from the stock server's own responses, from Nightscout's own
source files copied out of the running container, or from JavaScript itself (``Number()``, truthiness).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:1337"
NS_CONTAINER = "parity-ns"
MONGO_CONTAINER = "parity-mongo"
STATE_DIR = Path(os.environ.get("PARITY_STATE_DIR") or Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "ns-parity")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class Stock:
    """Access to the running stock server, its database and its source files."""

    def __init__(self) -> None:
        secret_file = STATE_DIR / "secret"
        if not secret_file.exists():
            raise RuntimeError(f"No stock server state in {STATE_DIR}; run tests_parity/stock.sh start <profile>")
        self.secret = secret_file.read_text()
        self.secret_sha1 = hashlib.sha1(self.secret.encode()).hexdigest()
        self.profile = (STATE_DIR / "profile").read_text()

    # ---- HTTP -------------------------------------------------------------

    def get_raw(self, path: str) -> bytes:
        request = urllib.request.Request(BASE_URL + path, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read()

    def get(self, path: str):
        return json.loads(self.get_raw(path))

    def post(self, path: str, body) -> None:
        request = urllib.request.Request(
            BASE_URL + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "api-secret": self.secret_sha1},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()

    # ---- database ---------------------------------------------------------

    def mongo(self, script: str) -> str:
        """Run JavaScript in the stock MongoDB shell against the Nightscout database."""
        result = subprocess.run(
            ["docker", "exec", "-i", MONGO_CONTAINER, "mongo", "nightscout", "--quiet"],
            input=script, capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"mongo shell failed: {result.stderr.strip()}")
        return result.stdout

    # ---- Nightscout's own source -------------------------------------------

    def copy_native(self, destination: Path) -> Path:
        """Copy the running server's lib/ directory out of the container, unchanged."""
        for app in ("/opt/app", "/app", "/usr/src/app"):
            probe = subprocess.run(
                ["docker", "exec", NS_CONTAINER, "test", "-f", f"{app}/lib/units.js"],
                capture_output=True, check=False,
            )
            if probe.returncode == 0:
                subprocess.run(["docker", "cp", f"{NS_CONTAINER}:{app}/lib", str(destination)], check=True)
                return destination / "lib"
        raise RuntimeError("Could not find Nightscout's lib/ directory in the stock container")


@pytest.fixture(scope="session")
def stock() -> Stock:
    return Stock()


@pytest.fixture(scope="session")
def native_lib(stock: Stock, tmp_path_factory) -> Path:
    return stock.copy_native(tmp_path_factory.mktemp("native"))


def node(script: Path, payload) -> object:
    """Run a Node script with a JSON payload on stdin and return its JSON output."""
    result = subprocess.run(
        ["node", str(script)], input=json.dumps(payload), capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{script.name} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def approved_exceptions() -> dict[str, str]:
    return json.loads((HERE / "exceptions.json").read_text())["approved"]


def report(mismatches: list, limit: int = 15) -> str:
    lines = [f"{len(mismatches)} mismatch(es) with stock Nightscout. First {min(limit, len(mismatches))}:"]
    lines += [f"  {json.dumps(m, default=str)}" for m in mismatches[:limit]]
    return "\n".join(lines)
