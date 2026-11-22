"""Fixtures and credential-safe result logging for the coverage subprocesses."""

from importlib import import_module
import os
from pathlib import Path
import stat

import pytest


SUITE = os.environ["NSX_COVERAGE_SUITE"]
LOG = Path(os.environ["NSX_COVERAGE_LOG"])
ROOT = Path.cwd().resolve()
MODULE = "custom_components.nightscout_extended"
FAILURE_TYPES = {
    "AssertionError", "ConnectionError", "FileNotFoundError", "ImportError",
    "IntegrationNotFound", "ModuleNotFoundError", "OSError", "PermissionError",
    "SocketBlockedError", "TimeoutError", "TypeError", "UnknownHandler", "ValueError",
}


def record(message):
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"{message}\n")


def check_config_dir(path, pytest_config):
    scratch = Path(pytest_config.getoption("basetemp")).resolve()
    config_dir = Path(path).resolve()
    if not config_dir.is_relative_to(scratch):
        record("HA config directory escaped the suite scratch directory")
        pytest.fail("HA config directory must be in the suite scratch directory")
    return config_dir


class WritableHAConfig:
    @pytest.fixture
    def hass_config_dir(self, hass_tmp_config_dir, request):
        config_dir = check_config_dir(hass_tmp_config_dir, request.config)
        # The native copy may have retained permission bits from its template.
        config_dir.chmod(config_dir.stat().st_mode | stat.S_IRWXU)
        return str(config_dir)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        if (hass := item.funcargs.get("hass")) is not None:
            check_config_dir(hass.config.config_dir, item.config)


def pytest_configure(config):
    if SUITE == "ha":
        # Register after native plugin autoload so this fixture wins the global lookup.
        config.pluginmanager.register(WritableHAConfig(), "nsx_coverage_ha_config")
    elif not config.pluginmanager.is_blocked("homeassistant"):
        record("API suite requires the HA pytest entry point to be blocked")
        raise pytest.UsageError("Unexpected HA plugin configuration")


def pytest_sessionstart(session):
    if SUITE == "ha":
        # pytest-cov is already active. Prime native HA discovery from this checkout.
        record("Loading the candidate HA integration")
        module = import_module(MODULE)
        source = (ROOT / "custom_components" / "nightscout_extended" / "__init__.py").resolve()
        if not source.is_relative_to(ROOT) or Path(module.__file__).resolve() != source:
            record("HA integration import came from outside the candidate checkout")
            raise pytest.UsageError("Unexpected integration source")
    record("Session started")


def pytest_collectreport(report):
    if report.failed:
        record(f"FAIL collect {report.nodeid.partition('[')[0]}")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.failed:
        # Pytest's automatic parameter IDs can contain fixture values. Preserve
        # the function and case indices without copying parameter representations.
        node = item.nodeid.partition("[")[0]
        if (case := getattr(item, "callspec", None)) is not None:
            indices = ",".join(str(case.indices[key]) for key in sorted(case.indices))
            node = f"{node} [case {indices}]"
        record(f"FAIL {report.when} {node}{failure_detail(call.excinfo)}")
    return report


def failure_detail(excinfo):
    if excinfo is None:
        return ""
    kind = excinfo.typename if excinfo.typename in FAILURE_TYPES else "Test exception"
    for entry in reversed(excinfo.traceback):
        path = Path(entry.path)
        if path.is_absolute() and path.is_relative_to(ROOT):
            return f" | {kind} at {path.relative_to(ROOT)}:{entry.lineno + 1}"
    return f" | {kind}"


def pytest_internalerror(excrepr, excinfo):
    record(f"FAIL internal{failure_detail(excinfo)}")


def pytest_sessionfinish(session, exitstatus):
    record(f"Collected {session.testscollected}, failed {session.testsfailed}, status {int(exitstatus)}")

    # Pytest assigns xfail/xpass before recording the terminal categories, and
    # setup/teardown successes use the empty category instead of "passed".
    stats = session.config.pluginmanager.get_plugin("terminalreporter").stats
    errors = stats.get("error", ())
    skips = stats.get("skipped", ())
    counts = {
        "passed": len(stats.get("passed", ())),
        "failed": len(stats.get("failed", ())),
        "skipped": sum(report.when != "collect" for report in skips),
        "xfailed": len(stats.get("xfailed", ())),
        "xpassed": len(stats.get("xpassed", ())),
        "errors": len(errors),
        "setup_errors": sum(report.when == "setup" for report in errors),
        "teardown_errors": sum(report.when == "teardown" for report in errors),
        "collection_errors": sum(report.when == "collect" for report in errors),
        "collection_skipped": sum(report.when == "collect" for report in skips),
        "deselected": len(stats.get("deselected", ())),
    }
    record("Outcomes " + " ".join(f"{name}={count}" for name, count in counts.items()))
