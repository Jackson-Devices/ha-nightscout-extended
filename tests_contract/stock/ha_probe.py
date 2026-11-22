"""Observe one exact Nightscout Extended checkout against a seeded stock server.

Invoke with the prepared Home Assistant Python. Read a private JSON request
on stdin and emit one credential-free JSON result on stdout. Exit 0 means
parity, 1 means a product mismatch, and 2 means the harness could not qualify
the mode. The server owner separately proves the equivalence of read rights.
"""

from contextlib import contextmanager
import hashlib
from importlib import import_module
from importlib.metadata import version
import ipaddress
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from ha_projection import (
    DOMAIN, MODES, PROBE_STATES, compare, compare_mode, expected_projection, instant,
    observe_calendar, observe_history, observe_state,
)


ROUTE_PATHS = {
    "/api/v1/status.json": "v1_status",
    "/api/v1/entries.json": "v1_entries",
    "/api/v1/entries/sgv.json": "v1_entries",
    "/api/v1/treatments.json": "v1_treatments",
    "/api/v1/devicestatus": "v1_devicestatus",
    "/api/v2/properties/bgnow": "v2_bgnow",
    "/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal": "v2_properties",
    "/api/v3/version": "v3_version",
    "/api/v3/entries": "v3_entries",
    "/api/v3/lastModified": "v3_last_modified",
    "/api/v3/treatments": "v3_treatments",
    "/api/v3/devicestatus": "v3_devicestatus",
}
PROTECTED_ROUTES = {"v3_entries", "v3_entries_history", "v3_last_modified", "v3_treatments", "v3_devicestatus"}
LEGACY_COLLECTIONS = {"v1_entries", "v1_treatments", "v1_devicestatus"}
CUSTOM_COMPONENTS = "custom_components"
PACKAGE = f"{CUSTOM_COMPONENTS}.{DOMAIN}"


def _route(path):
    if path.startswith("/api/v2/authorization/request/"):
        return "v2_subject_exchange"
    if path.startswith("/api/v3/entries/history/"):
        return "v3_entries_history"
    return ROUTE_PATHS.get(path.rstrip("/"), "unexpected")


def _launch():
    request = json.load(sys.stdin)
    mode = request["mode"]
    if mode not in MODES:
        raise ValueError("invalid mode")
    credential = request.get("credential")
    if mode == "candidate_v3":
        if not isinstance(credential, str) or not credential or any(char.isspace() for char in credential):
            raise ValueError("invalid private v3 input")
    elif credential is not None:
        raise ValueError("legacy modes expect public reads")
    address = urlsplit(request["url"])
    if (
        address.scheme != "http" or not ipaddress.ip_address(address.hostname).is_loopback
        or address.port is None or address.path not in ("", "/")
        or address.username is not None or address.password is not None
        or address.query or address.fragment
    ):
        raise ValueError("a bare disposable loopback origin is required")
    product = Path(request["product_root"])
    if not product.is_absolute():
        raise ValueError("an absolute product root is required")
    root = product.resolve(strict=True)
    component = (root / "custom_components" / DOMAIN).resolve(strict=True)
    if not component.is_relative_to(root) or not (component / "manifest.json").is_file():
        raise ValueError("the product checkout must contain its own integration")
    now = instant(request["observed_at"])
    if now.microsecond % 1000:
        raise ValueError("the observation instant must be millisecond-aligned")
    expected = expected_projection(request["expected"], now)
    return request, component, now, expected


@contextmanager
def _discard_runtime_output():
    """Keep pytest, HA, and third-party logs and tracebacks off both inherited pipes."""
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "wb") as sink:
        saved = [os.dup(fd) for fd in (1, 2)]
        try:
            for fd in (1, 2):
                os.dup2(sink.fileno(), fd)
            yield
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                for fd, previous in zip((1, 2), saved, strict=True):
                    os.dup2(previous, fd)
                    os.close(previous)


if __name__ != "__main__":
    import pytest

    # Module-local fixtures override the installed HA plugin defaults.
    @pytest.fixture
    def hass_config_dir(probe_bundle):
        _, config, _ = probe_bundle
        return str(config)

    @pytest.fixture
    def hass_storage(monkeypatch):
        # Keep genuine disk storage for product data. Only HA's config entries,
        # whose real flow contains the ephemeral subject, stay unwritten.
        from homeassistant.helpers.storage import Store

        write = Store._async_write_data

        async def write_except_config_entries(store, data):
            if store.key != "core.config_entries":
                await write(store, data)

        monkeypatch.setattr(Store, "_async_write_data", write_except_config_entries)
        return {}


def _route_callback(launch, records):
    from aiohttp import TraceConfig
    from yarl import URL

    configured_origin = URL(launch["url"]).origin()

    async def request_start(_session, _context, params):
        if params.url.origin() != configured_origin or params.method != "GET":
            raise RuntimeError("unexpected origin or method in read-only HA probe")

    def record_response(params):
        headers = params.headers
        record = {
            "route": _route(params.url.path),
            "http_status": params.response.status,
            "bearer_present": bool(headers.get("Authorization", "").startswith("Bearer ")),
            "subject_query_present": "token" in params.url.query,
            "api_secret_present": "api-secret" in headers,
        }
        if record not in records:
            records.append(record)

    async def request_end(_session, _context, params):
        record_response(params)

    async def request_redirect(_session, _context, params):
        record_response(params)
        raise RuntimeError("unexpected redirect in read-only HA probe")

    trace = TraceConfig()
    trace.on_request_start.append(request_start)
    trace.on_request_end.append(request_end)
    trace.on_request_redirect.append(request_redirect)
    trace.freeze()
    return trace


def _mode_checks(mode, records):
    routes = {row["route"] for row in records}
    success = {row["route"] for row in records if row["http_status"] == 200}
    required = {"v1_status", "v2_bgnow", "v2_properties"}
    v3 = mode == "candidate_v3"
    if v3:
        required |= {"v3_version", "v2_subject_exchange", "v3_entries", "v3_last_modified", "v3_treatments", "v3_devicestatus"}
    else:
        required.add("v1_treatments")
        if mode == "candidate_legacy":
            required |= {"v1_entries", "v1_devicestatus"}

    return {
        "required_routes_ok": required <= success,
        "known_routes_only": "unexpected" not in routes,
        "collection_family_matches_mode": (
            not (routes & LEGACY_COLLECTIONS) if v3
            else not (routes & (PROTECTED_ROUTES | {"v3_version", "v2_subject_exchange"}))
        ),
        "request_auth_matches_mode": all(
            not row["api_secret_present"]
            and row["bearer_present"] == (v3 and row["route"] in PROTECTED_ROUTES)
            and row["subject_query_present"] == (v3 and row["route"] in {"v1_status", "v2_bgnow", "v2_properties"})
            for row in records
        ),
    }


def _source_proof(component):
    # Modules are imported through HA's real loader. Never fall back to imports
    # from the QA tree, an installed custom copy, or a neighboring checkout.
    files = {}
    for name, module in tuple(sys.modules.items()):
        if name != PACKAGE and not name.startswith(f"{PACKAGE}."):
            continue
        file = Path(module.__file__).resolve(strict=True)
        relative = file.relative_to(component).as_posix()
        files[relative] = hashlib.sha256(file.read_bytes()).hexdigest()
    return files


def _stage_custom_components(config, component):
    """Give HA an isolated package containing only the exact staged product."""
    namespace = config / CUSTOM_COMPONENTS
    namespace.mkdir()
    initializer = namespace / "__init__.py"
    initializer.touch()
    (namespace / DOMAIN).symlink_to(component, target_is_directory=True)

    if CUSTOM_COMPONENTS in sys.modules:
        raise RuntimeError("custom_components was imported before probe staging")
    config_path = str(config)
    sys.path.insert(0, config_path)
    try:
        package = import_module(CUSTOM_COMPONENTS)
    finally:
        sys.path.remove(config_path)
        sys.path_importer_cache.pop(config_path, None)
    if (
        Path(package.__file__).resolve(strict=True) != initializer
        or tuple(Path(path).resolve(strict=True) for path in package.__path__) != (namespace,)
    ):
        raise RuntimeError("custom_components resolved outside probe staging")


async def test_private_stock_mode(hass, freezer, enable_custom_integrations, socket_enabled, probe_bundle):
    """Load a normal config entry, observe native HA outputs, and unload it."""
    from homeassistant import config_entries, loader
    from homeassistant.const import CONF_API_KEY, CONF_URL
    from homeassistant.data_entry_flow import FlowResultType
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from pytest_socket import socket_allow_hosts

    (launch, component, now, reference), config, result = probe_bundle
    mode = launch["mode"]
    attestation = result["attestation"]
    attestation["routes"] = []
    attestation["storage"] = "fresh_disk_except_ha_config_entries"
    attestation["observed_at"] = now.isoformat()
    result["error_phase"] = "ha_config_dir"
    attestation["exact_temp_config_dir"] = Path(hass.config.config_dir).resolve(strict=True) == config
    if not attestation["exact_temp_config_dir"]:
        return
    socket_allow_hosts([urlsplit(launch["url"]).hostname])
    freezer.move_to(now)
    await hass.config.async_set_time_zone("UTC")
    client = async_get_clientsession(hass)
    client.trace_configs.append(_route_callback(launch, attestation["routes"]))

    result["error_phase"] = "ha_product_import"
    product = await hass.async_add_executor_job(import_module, PACKAGE)
    attestation["exact_product_package"] = (
        Path(product.__file__).resolve(strict=True) == component / "__init__.py"
        and tuple(Path(path).resolve(strict=True) for path in product.__path__) == (component,)
    )
    if not attestation["exact_product_package"]:
        return

    result["error_phase"] = "ha_loader"
    integration = await loader.async_get_integration(hass, DOMAIN)
    attestation["exact_product_root"] = (
        integration.pkg_path == PACKAGE and not integration.is_built_in
        and integration.file_path.resolve(strict=True) == component
    )
    if not attestation["exact_product_root"]:
        return
    attestation["manifest_sha256"] = hashlib.sha256((component / "manifest.json").read_bytes()).hexdigest()

    result["error_phase"] = "ha_config_flow"
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    if form["type"] is not FlowResultType.FORM:
        return
    user_input = {CONF_URL: launch["url"]}
    if launch.get("credential") is not None:
        user_input[CONF_API_KEY] = launch["credential"]
    outcome = await hass.config_entries.flow.async_configure(form["flow_id"], user_input)
    entry = outcome.get("result") if outcome["type"] is FlowResultType.CREATE_ENTRY else None
    entry_state = config_entries.ConfigEntryState
    attestation["config_entry_created"] = entry is not None
    await hass.async_block_till_done()
    attestation["entry_loaded"] = entry is not None and entry.state is entry_state.LOADED
    if not attestation["entry_loaded"]:
        if not attestation["routes"]:
            return
        result["mismatches"] = compare({"entry_loaded": True}, attestation, "attestation")
        result["status"] = "mismatch"
        result.pop("error_phase")
        return

    try:
        result["error_phase"] = "ha_entities"
        registry = er.async_get(hass)
        rows = {
            row.translation_key: row for row in er.async_entries_for_config_entry(registry, entry.entry_id)
            if row.platform == DOMAIN
        }
        # Basal is installed with the normal registry opt-in in both subjects.
        basal = rows.get("basal")
        attestation["basal_enabled_for_probe"] = basal is not None and basal.disabled_by is not None
        if attestation["basal_enabled_for_probe"]:
            registry.async_update_entity(basal.entity_id, disabled_by=None)
            if not await hass.config_entries.async_reload(entry.entry_id):
                result["mismatches"] = compare(True, False, "attestation.basal_reload_ok")
                result["status"] = "mismatch"
                return
            await hass.async_block_till_done()

        attestation["uses_v3"] = getattr(entry.runtime_data, "uses_v3", False)
        attestation["source_files_sha256"] = _source_proof(component)
        common_files = {"__init__.py", "api.py", "calendar.py", "config_flow.py", "sensor.py"}
        if not common_files <= attestation["source_files_sha256"].keys():
            result["error_phase"] = "ha_source_provenance"
            return

        actual = {"states": {}}
        result["actual"] = actual
        for role in PROBE_STATES:
            state = hass.states.get(rows[role].entity_id) if role in rows else None
            actual["states"][role] = observe_state(role, state, reference["states"][role]["attributes"])

        result["error_phase"] = "ha_public_services"
        authority = launch["expected"]
        actual["calendar"] = None
        calendar = rows.get("treatments")
        if calendar is not None:
            start, end = authority["calendar_window"]
            try:
                response = await hass.services.async_call(
                    "calendar", "get_events", {
                        "entity_id": calendar.entity_id,
                        "start_date_time": instant(start), "end_date_time": instant(end),
                    }, blocking=True, return_response=True,
                )
            except HomeAssistantError:
                attestation["calendar_service_error"] = True
            else:
                events = response.get(calendar.entity_id, {}).get("events") if isinstance(response, dict) else None
                actual["calendar"] = observe_calendar(events, reference["calendar"])

        actual["history"] = None
        history_supported = hass.services.has_service(DOMAIN, "get_glucose_history")
        attestation["has_glucose_history_service"] = history_supported
        if history_supported and "glucose" in rows:
            start, end = authority["history_window"]
            try:
                history = await hass.services.async_call(
                    DOMAIN, "get_glucose_history", {"entity_id": rows["glucose"].entity_id, "start": start, "end": end},
                    blocking=True, return_response=True,
                )
            except HomeAssistantError:
                attestation["history_service_error"] = True
            else:
                actual["history"] = observe_history(history)

        attest_checks = _mode_checks(mode, attestation["routes"])
        attestation.update(attest_checks)
        mismatches, absent = compare_mode(reference, actual, mode, history_supported)
        result["absent_capabilities"] = absent
        result["raw_only_fields"] = ["intake.rate"]
        mismatches.extend(compare({key: True for key in attest_checks}, attestation, "attestation"))
        mismatches.extend(compare({"uses_v3": mode == "candidate_v3"}, attestation, "attestation"))
        result["mismatches"] = mismatches
        result["status"] = "mismatch" if mismatches else "pass"
        result.pop("error_phase")
    finally:
        if entry.state is entry_state.LOADED:
            attestation["entry_unloaded"] = await hass.config_entries.async_unload(entry.entry_id)
        else:
            attestation["entry_unloaded"] = entry.state is entry_state.NOT_LOADED
        await hass.async_block_till_done()
        if not attestation["entry_unloaded"]:
            result["status"] = "harness_error"
            result["error_phase"] = "ha_unload"


def _run(pytest, bundle, result):
    _launch, component, _now, _expected = bundle
    sys.dont_write_bytecode = True
    with TemporaryDirectory(prefix="nsx-stock-ha-") as temp:
        work = Path(temp).resolve(strict=True)
        config = work / "configuration"
        config.mkdir()
        result["error_phase"] = "ha_namespace_staging"
        _stage_custom_components(config, component)
        result["attestation"]["isolated_custom_components"] = True
        ini = work / "pytest.ini"
        ini.write_text("[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n", encoding="utf-8")
        selection = f"{Path(__file__).resolve()}::test_private_stock_mode"
        result["error_phase"] = "ha_fixture_setup"

        class ProbeInput:
            @pytest.fixture
            def probe_bundle(self):
                return bundle, config, result

        return pytest.main(
            ["-q", "--tb=no", "--capture=no", "--import-mode=importlib", "--noconftest", "-c", str(ini),
             "-p", "no:cacheprovider", "--basetemp", str(work / "pytest"), selection],
            plugins=["pytest_homeassistant_custom_component", ProbeInput()],
        )


def main():
    result = {"schema_version": 1, "mode": None, "status": "harness_error", "actual": {},
              "mismatches": [], "absent_capabilities": [], "attestation": {}, "error_phase": "input"}
    credential = None
    try:
        with _discard_runtime_output():
            bundle = _launch()
            launch = bundle[0]
            result["mode"] = launch["mode"]
            credential = launch.get("credential")
            result["error_phase"] = "ha_dependencies"
            import pytest

            result["attestation"]["versions"] = {
                name: version(name) for name in ("homeassistant", "pytest-homeassistant-custom-component", "pytest")
            }
            result["error_phase"] = "ha_fixture_setup"
            pytest_status = _run(pytest, bundle, result)
            if pytest_status:
                result["status"] = "harness_error"
                result.setdefault("error_phase", "pytest_teardown")
                result["pytest_exit"] = int(pytest_status)
    except (Exception, KeyboardInterrupt):
        result["status"] = "harness_error"
        result.setdefault("error_phase", "probe_launcher")

    wire = json.dumps(result, allow_nan=False, sort_keys=True)
    if credential and (credential in wire or json.dumps(credential)[1:-1] in wire):
        result = {"schema_version": 1, "mode": result["mode"], "status": "harness_error", "actual": {},
                  "mismatches": [], "absent_capabilities": [], "attestation": {}, "error_phase": "output_projection"}
        wire = json.dumps(result, sort_keys=True)
    print(wire)
    return {"pass": 0, "mismatch": 1, "harness_error": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
