"""Private subprocess entry point for one installed HA product against live stock."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
from importlib import import_module
import json
import math
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

DOMAIN = "nightscout_extended"
PACKAGE = f"custom_components.{DOMAIN}"
LOOPBACK = "http://127.0.0.1:1337"
SENSOR_FIELDS = {
    "glucose": ("direction", "delta", "thresholds_mgdl"),
    "glucose_delta": (),
    "glucose_trend": (),
    "last_reading": (),
    "iob": ("source",),
    "cob": ("source",),
    "basal": (),
    "last_carbs": ("time", "event_type", "notes"),
    "last_insulin": ("time", "event_type", "notes"),
    "forecast_15": ("source_time", "forecast_time"),
    "forecast_30": ("source_time", "forecast_time", "forecast", "model"),
    "insulin_remaining": ("source_time", "uploader"),
    "insulin_age": ("source_time", "event_type"),
    "cannula_age": ("source_time", "event_type"),
    "sensor_age": ("source_time", "event_type"),
    "pump_battery_age": ("source_time", "event_type"),
}
TIME_FIELDS = {"time", "source_time", "forecast_time"}
ROUTES = {
    "/api/v1/status.json": "v1.status",
    "/api/v1/entries.json": "v1.entries",
    "/api/v1/treatments.json": "v1.treatments",
    "/api/v1/devicestatus": "v1.devicestatus",
    "/api/v2/properties/bgnow": "v2.bgnow",
    "/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal": "v2.properties",
    "/api/v3/version": "v3.version",
    "/api/v3/entries": "v3.entries",
    "/api/v3/treatments": "v3.treatments",
    "/api/v3/devicestatus": "v3.devicestatus",
    "/api/v3/lastModified": "v3.last_modified",
}
V3_PROTECTED = {"v3.entries", "v3.treatments", "v3.devicestatus", "v3.last_modified", "v3.history"}
EXCHANGE = "/api/v2/authorization/request/"
HISTORY = "/api/v3/entries/history/"
UNKNOWN = "<unexpected>"
ID = re.compile(r"(?:[a-fA-F0-9]{24}|[a-fA-F0-9-]{36})\Z")


def _time(value):
    if value is None:
        return None
    try:
        when = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if when.utcoffset() is not None:
            return when.astimezone(UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    return UNKNOWN


def _number(value, *, text=False):
    if value is None:
        return None
    try:
        if text and isinstance(value, str):
            value = float(value)
        if type(value) in (int, float) and math.isfinite(value):
            return value
    except (TypeError, ValueError, OverflowError):
        pass
    return UNKNOWN


def _id(value):
    return value if isinstance(value, str) and ID.fullmatch(value) else UNKNOWN


def _text(value, allowed):
    return value if value is None or isinstance(value, str) and value in allowed else UNKNOWN


def _state(hass, row, role, allowed):
    state = hass.states.get(row.entity_id) if row else None
    if state is None:
        return None
    value = state.state
    if value not in {"unknown", "unavailable"}:
        if role == "last_reading":
            value = _time(value)
        elif role == "glucose_trend":
            value = _text(value, allowed)
        else:
            value = _number(value, text=True)
    output = {"value": value, "unit": _text(state.attributes.get("unit_of_measurement"), allowed)}
    for key in SENSOR_FIELDS[role]:
        if key not in state.attributes:
            continue
        attr = state.attributes[key]
        if key in TIME_FIELDS:
            output[key] = _time(attr)
        elif key == "delta":
            output[key] = _number(attr)
        elif key == "forecast" and isinstance(attr, list):
            output[key] = [{"time": _time(row.get("time")), "mgdl": _number(row.get("mgdl"))} for row in attr]
        elif key == "thresholds_mgdl" and isinstance(attr, dict):
            output[key] = {
                name: _number(attr[name]) for name in ("urgent_low", "low", "high", "urgent_high") if name in attr
            }
        else:
            output[key] = _text(attr, allowed)
    return output


def _trace(origin, subject, records):
    """Attest live HTTP response status and exact auth transport without retaining auth material."""
    from aiohttp import TraceConfig

    digest = hashlib.sha1(subject.encode()).hexdigest()

    async def started(_session, _context, params):
        if str(params.url.origin()) != origin or params.method != "GET":
            raise RuntimeError("candidate issued a non-stock or mutating request")

    async def ended(_session, _context, params):
        path = params.url.path
        if path.startswith(EXCHANGE):
            route = "v2.exchange" if path == EXCHANGE + subject else "unexpected"
        elif path.startswith(HISTORY):
            route = "v3.history"
        else:
            route = ROUTES.get(path.rstrip("/"), "unexpected")
        query = params.url.query
        headers = params.headers
        if "api-secret" in headers and "token" not in query and "Authorization" not in headers:
            auth = "subject_digest" if headers["api-secret"] == digest else "unexpected"
        elif "token" in query and "Authorization" not in headers and "api-secret" not in headers:
            auth = "subject_query" if query.getall("token") == [subject] else "unexpected"
        elif "Authorization" in headers and "token" not in query and "api-secret" not in headers:
            auth = "bearer" if headers["Authorization"].startswith("Bearer ") else "unexpected"
        elif "Authorization" not in headers and "api-secret" not in headers and "token" not in query:
            auth = "none"
        else:
            auth = "unexpected"
        records.add((route, params.response.status, auth))

    async def redirected(session, context, params):
        await ended(session, context, params)
        raise RuntimeError("candidate stock request redirected")

    trace = TraceConfig()
    trace.on_request_start.append(started)
    trace.on_request_end.append(ended)
    trace.on_request_redirect.append(redirected)
    trace.freeze()
    return trace


def _source_hashes(component):
    sources = {}
    for name, module in tuple(sys.modules.items()):
        if name == PACKAGE or name.startswith(f"{PACKAGE}."):
            path = Path(module.__file__).resolve(strict=True)
            sources[path.relative_to(component).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return sources


if __name__ != "__main__":
    import pytest

    @pytest.fixture
    def hass_config_dir(candidate_input):
        return str(candidate_input[1])

    @pytest.fixture
    def hass_storage(monkeypatch):
        # Use real product storage. Keep native config-entry credentials out of disk snapshots.
        from homeassistant.helpers.storage import Store

        write = Store._async_write_data

        async def write_without_entry(store, data):
            if store.key != "core.config_entries":
                await write(store, data)

        monkeypatch.setattr(Store, "_async_write_data", write_without_entry)
        return {}


async def test_private_candidate(socket_enabled, hass, freezer, enable_custom_integrations, candidate_input):
    """Exercise HA's config entry setup, real coordinator and public services."""
    from homeassistant import loader
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.const import CONF_API_KEY, CONF_URL
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_socket import socket_allow_hosts

    setup, config, component, result, records = candidate_input
    socket_allow_hosts([urlsplit(LOOPBACK).hostname], allow_unix_socket=True)
    freezer.move_to(datetime.fromisoformat(setup["clock"]))
    await hass.config.async_set_time_zone("UTC")
    async_get_clientsession(hass).trace_configs.append(_trace(LOOPBACK, setup["subject"], records))

    result["phase"] = "product_source"
    assert Path(hass.config.config_dir) == config
    integration = await loader.async_get_integration(hass, DOMAIN)
    assert integration.pkg_path == PACKAGE and not integration.is_built_in
    assert (await hass.async_add_executor_job(integration.file_path.resolve, True)) == component
    constants = await hass.async_add_executor_job(import_module, f"{PACKAGE}.const")
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=LOOPBACK, title="Stock version parity",
        data={CONF_URL: LOOPBACK, CONF_API_KEY: setup["subject"],
              constants.CONF_USE_API_SECRET: setup["mode"] == "legacy"},
    )
    entry.add_to_hass(hass)
    result["phase"] = "config_entry_setup"
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        registry = er.async_get(hass)
        entities = {row.translation_key: row for row in er.async_entries_for_config_entry(registry, entry.entry_id)
                    if row.platform == DOMAIN}
        basal = entities.get("basal")
        if basal is not None and basal.disabled_by is not None:
            registry.async_update_entity(basal.entity_id, disabled_by=None)
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()

        coordinator = entry.runtime_data
        result["uses_v3"] = coordinator.uses_v3
        result["uses_legacy_header"] = coordinator.client.uses_api_secret
        data = coordinator.data
        allowed = set(setup["text"])
        result["phase"] = "sensors"
        result["observation"] = {
            "raw_current": {"sgv": _number(data.glucose), "time": _time(data.glucose_time)},
            "states": {role: _state(hass, entities.get(role), role, allowed) for role in SENSOR_FIELDS},
            "treatments": [
                {"id": _id(row.uid), "time": _time(row.time), "event_type": _text(row.event_type, allowed),
                 "notes": _text(row.notes, allowed), "entered_by": _text(row.entered_by, allowed),
                 "carbs": _number(row.carbs), "insulin": _number(row.insulin), "duration": _number(row.duration_min)}
                for row in data.treatments if row.uid in setup["treatment_ids"] or row.entered_by == setup["actor"]
            ],
        }
        await _services(hass, entities, setup, result, allowed)

        result["phase"] = "product_module_attestation"
        sources = await hass.async_add_executor_job(_source_hashes, component)
        result["sources"] = sources
        required = {"__init__.py", "api.py", "calendar.py", "coordinator.py", "sensor.py"}
        assert required <= sources.keys()
    finally:
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
    result["phase"] = "complete"


async def _services(hass, entities, setup, result, allowed):
    observation = result["observation"]
    result["phase"] = "calendar_service"
    observation["calendar"] = None
    if "treatments" in entities:
        calendar = entities["treatments"].entity_id
        start, end = map(datetime.fromisoformat, setup["calendar_range"])
        response = await hass.services.async_call(
            "calendar", "get_events",
            {"entity_id": calendar, "start_date_time": start, "end_date_time": end},
            blocking=True, return_response=True,
        )
        events = response[calendar]["events"]
        known_times = set(setup["calendar_times"])
        marker = "\nEntered by " + setup["actor"]
        observation["calendar"] = [
            {"start": _time(row.get("start")), "end": _time(row.get("end")),
             "summary": _text(row.get("summary"), allowed), "description": _text(row.get("description"), allowed)}
            for row in events
            if _time(row.get("start")) in known_times or (row.get("description") or "").endswith(marker)
        ]

    result["phase"] = "history_service"
    observation["history"] = None
    if "glucose" in entities and hass.services.has_service(DOMAIN, "get_glucose_history"):
        start, end = setup["history_range"]
        history = await hass.services.async_call(
            DOMAIN, "get_glucose_history", {"entity_id": entities["glucose"].entity_id, "start": start, "end": end},
            blocking=True, return_response=True,
        )
        observation["history"] = {
            "unit": _text(history.get("unit"), allowed),
            "status": _text(history.get("status"), allowed),
            "as_of": _time(history.get("as_of")),
            "missing_ranges": [{"start": _number(row.get("start")), "end": _number(row.get("end"))}
                               for row in history["missing_ranges"]],
            "readings": [{"id": _id(row.get("id")),
                          "date": row["date"] if type(row.get("date")) is int else UNKNOWN,
                          "sgv": _number(row.get("sgv")), "direction": _text(row.get("direction"), allowed)}
                         for row in history["readings"]],
        }


@contextmanager
def _quiet():
    """Prevent native pytest or HA output from publishing request data."""
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "wb") as sink:
        saved = [os.dup(fd) for fd in (1, 2)]
        try:
            for fd in (1, 2):
                os.dup2(sink.fileno(), fd)
            yield
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            for fd, previous in zip((1, 2), saved, strict=True):
                os.dup2(previous, fd)
                os.close(previous)


def _run(request, result, routes):
    import pytest

    root = Path(request["product_root"]).resolve(strict=True)
    component = (root / "custom_components" / DOMAIN).resolve(strict=True)
    if not component.is_relative_to(root) or not (component / "manifest.json").is_file():
        raise ValueError("product root does not contain its integration")
    with TemporaryDirectory(prefix="nightscout-parity-") as directory:
        temp = Path(directory).resolve()
        config = temp / "config"
        namespace = config / "custom_components"
        namespace.mkdir(parents=True)
        (namespace / "__init__.py").touch()
        (namespace / DOMAIN).symlink_to(component, target_is_directory=True)
        if "custom_components" in sys.modules:
            raise RuntimeError("custom integration imported before source isolation")
        sys.path.insert(0, str(config))
        try:
            package = import_module("custom_components")
        finally:
            sys.path.remove(str(config))
            sys.path_importer_cache.pop(str(config), None)
        if tuple(Path(path).resolve() for path in package.__path__) != (namespace,):
            raise RuntimeError("unexpected custom integration package")
        ini = temp / "pytest.ini"
        ini.write_text("[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n", encoding="utf-8")

        class ProbeInput:
            @pytest.fixture
            def candidate_input(self):
                return request, config, component, result, routes

        return pytest.main(
            ["-q", "--noconftest", "--import-mode=importlib", "--tb=no", "--capture=no", "-c", str(ini),
             "-p", "no:cacheprovider", "--basetemp", str(temp / "pytest"),
             f"{Path(__file__).resolve()}::test_private_candidate"],
            plugins=["pytest_homeassistant_custom_component", ProbeInput()],
        )


def main():
    result = {"phase": "private_input"}
    routes = set()
    request = None
    try:
        with _quiet():
            request = json.load(sys.stdin)
            subject = request["subject"]
            if request["mode"] not in {"legacy", "v3"} or not isinstance(subject, str) or not subject:
                raise ValueError("invalid private probe mode or subject")
            result["phase"] = "fixture_setup"
            result["pytest_exit"] = int(_run(request, result, routes))
    except Exception as exc:
        result["launcher_error"] = type(exc).__name__
    result["routes"] = sorted(routes)
    output = json.dumps(result, allow_nan=False)
    if isinstance(request, dict) and isinstance(subject := request.get("subject"), str) and subject:
        digest = hashlib.sha1(subject.encode()).hexdigest()
        if subject in output or json.dumps(subject)[1:-1] in output or digest in output:
            output = json.dumps({"phase": "output_redaction"})
    print(output)


if __name__ == "__main__":
    main()
