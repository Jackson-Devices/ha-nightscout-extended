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
import pytest_socket

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


@pytest.fixture(autouse=True)
def allow_local_stock_server(socket_enabled):
    """Home Assistant's test plugin blocks sockets. Allow them; connections stay limited to 127.0.0.1."""
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    yield


class Stock:
    """Access to the running stock server, its database and its source files."""

    def __init__(self) -> None:
        secret_file = STATE_DIR / "secret"
        if not secret_file.exists():
            raise RuntimeError(f"No stock server state in {STATE_DIR}; run tests_parity/stock.sh start <profile>")
        self.secret = secret_file.read_text()
        self.profile = (STATE_DIR / "profile").read_text()
        # Session fixtures are created before the per-test socket fixture runs.
        pytest_socket.enable_socket()
        pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)

    @property
    def setup_headers(self) -> dict[str, str]:
        """15.0.6 lib/server/enclave.js accepts a SHA-512 hex digest for fixture setup."""
        return {"api-secret": hashlib.sha512(self.secret.encode()).hexdigest()}

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
            headers={"Content-Type": "application/json", **self.setup_headers},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()

    def delete(self, path: str) -> None:
        request = urllib.request.Request(BASE_URL + path, headers=self.setup_headers, method="DELETE")
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


def report(mismatches: list, limit: int = 8) -> str:
    """Summarise mismatches by kind, with up to `limit` examples of each kind."""
    groups: dict[str, list] = {}
    for item in mismatches:
        groups.setdefault(str(item.get("what", item.get("field", "mismatch"))), []).append(item)
    lines = [f"{len(mismatches)} mismatch(es) with stock Nightscout, in {len(groups)} kind(s):"]
    for kind, items in groups.items():
        lines.append(f"- {kind}: {len(items)}")
        lines += [f"    {json.dumps(item, default=str, ensure_ascii=False)}" for item in items[:limit]]
    return "\n".join(lines)
