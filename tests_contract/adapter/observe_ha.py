"""Observe the Nightscout integration through a Home Assistant test instance."""

import asyncio
from concurrent.futures import Future
from copy import deepcopy
from datetime import timezone
from urllib.parse import urlsplit

from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.config_entries import ConfigEntryState, ConfigEntryStore
from homeassistant.const import CONF_API_KEY, CONF_URL, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import DATA_CUSTOM_COMPONENTS
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

from custom_components.nightscout_extended.const import CONF_USE_API_SECRET, DOMAIN


ROLE_KEYS = {
    "last_insulin": "latest_bolus",
    "last_carbs": "latest_carbs",
    "treatments": "calendar",
}
IDENTITY_ROLES = {"glucose", "latest_bolus", "latest_carbs", "calendar"}
MAINTENANCE_ROLES = ("insulin_age", "site_age", "sensor_age", "battery_age")
OPERATIONS = {
    "glucose": ("glucose",),
    "version": ("api_version",),
    "derived": ("iob", "cob", "basal", "site_settings"),
    "site": ("site_settings",),
    "forecast": ("forecast_15", "forecast_30"),
    "treatments": ("latest_bolus", "latest_carbs", "calendar"),
    "reservoir": ("insulin_remaining",),
    "recovery": ("glucose", "site_settings"),
    "identity": ("glucose", "latest_bolus", "latest_carbs", "calendar", "site_settings", "registry"),
}


def _role(row):
    key = row.translation_key
    if key is None:
        # Legacy fixture rows identify their role in the stored unique id.
        _, _, key = row.unique_id.partition("/")
        key = key.removesuffix("/v2")
    return ROLE_KEYS.get(key, key)


def _utc_iso(value):
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sensor(state, row, time=None, attributes=()):
    if state is None:
        return None
    unit = state.attributes.get("unit_of_measurement", row.unit_of_measurement)
    if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        details = {}
        if "http_status" in state.attributes:
            details["http_status"] = state.attributes["http_status"]
        return [None, unit, None, state.state, details]
    try:
        value = float(state.state)
    except ValueError:
        value = state.state
    details = {key: state.attributes[key] for key in attributes if key in state.attributes}
    return [value, unit, _utc_iso(time), "available", details]


def _entry_data(url, credentials):
    # The released config entry has one input for a cleartext API secret or a
    # query token. Preserve the supplied raw input. A pre-hashed secret has no
    # equivalent input on this release and must not be hashed a second time.
    cleartext_secret = credentials.get("api_secret")
    token = next(
        (credentials[key] for key in ("query_token", "subject_token", "legacy_bearer", "initial_bearer")
         if key in credentials),
        None,
    )
    return {
        CONF_URL: url,
        CONF_API_KEY: cleartext_secret or token,
        CONF_USE_API_SECRET: cleartext_secret is not None,
    }


class HomeAssistantObserver:
    """Use real config entries, storage, network I/O, and HA public observations."""

    def __init__(self, freezer, storage, config_dir):
        self.freezer = freezer
        self.storage = storage
        self.config_dir = str(config_dir)
        self.hass = None
        self.context = None
        self.entry = None
        self.initial_state = {}
        self.lifecycle = None
        self.entry_state = None
        self.observations = []

    def load_state(self, state):
        self.initial_state = state

    async def dump_state(self):
        if self.hass is None:
            return {}
        await self._save_registry()
        if self.entry is not None:
            await ConfigEntryStore(self.hass).async_save({"entries": [self.entry.as_dict()]})
        return {"storage": deepcopy(self.storage)}

    async def _save_registry(self):
        registry = er.async_get(self.hass)
        # Force a checkpoint of the real store before replacing the HA runtime,
        # instead of waiting for the registry's delayed save on the test clock.
        await registry._store.async_save(registry._data_to_save())

    async def _seed_registry(self, seeds):
        registry = er.async_get(self.hass)
        for seed in seeds:
            row = registry.async_get_or_create(
                seed["domain"], seed["platform"], seed["unique_id"],
                suggested_object_id=seed["entity_id"].split(".", 1)[1],
                config_entry=self.entry,
            )
            registry.async_update_entity(row.entity_id, name=seed["name"], aliases=seed["aliases"])
        await self._save_registry()
        ids = {seed["entity_id"]: seed["id"] for seed in seeds}
        for row in self.storage[er.STORAGE_KEY]["data"]["entities"]:
            row["id"] = ids[row["entity_id"]]
        # Read fixture-provided opaque IDs through the native store and index.
        await registry.async_load()

    async def _start(self, url, request):
        self.storage.clear()
        self.storage.update(deepcopy(self.initial_state.get("storage", {})))
        self.context = async_test_home_assistant(
            load_registries=False, config_dir=self.config_dir
        )
        self.hass = await self.context.__aenter__()
        self.hass.data.pop(DATA_CUSTOM_COMPONENTS)
        if not await async_load_base_functionality(self.hass):
            raise RuntimeError("Home Assistant could not initialize its native stores")
        entries = self.hass.config_entries.async_entries(DOMAIN)
        entry_data = _entry_data(url, request["credentials"])
        if entries:
            self.entry, = entries
            self.hass.config_entries.async_update_entry(self.entry, data=entry_data)
        else:
            self.entry = MockConfigEntry(
                domain=DOMAIN, unique_id=url, title="Contract Nightscout", data=entry_data,
            )
            self.entry.add_to_hass(self.hass)
            if "registry" in self.initial_state:
                await self._seed_registry(self.initial_state["registry"])
        # This invokes HA's config-entry setup and version handling with the
        # actual installed integration. The fixture generation is not a
        # Home Assistant config-entry schema version.
        if request["lifecycle"] == "reload":
            await self.hass.config_entries.async_reload(self.entry.entry_id)
        else:
            await self.hass.config_entries.async_setup(self.entry.entry_id)

    async def _poll(self, url, request):
        entry_data = _entry_data(url, request["credentials"])
        if dict(self.entry.data) != entry_data:
            loaded = self.entry.state is ConfigEntryState.LOADED
            self.hass.config_entries.async_update_entry(self.entry, data=entry_data)
            if not loaded:
                await self.hass.config_entries.async_reload(self.entry.entry_id)
        elif request["lifecycle"] == "reload":
            await self.hass.config_entries.async_reload(self.entry.entry_id)
        elif self.entry.state is ConfigEntryState.LOADED:
            await self.entry.runtime_data.async_refresh()
        else:
            await self.hass.config_entries.async_reload(self.entry.entry_id)

    def _rows(self):
        return er.async_entries_for_config_entry(er.async_get(self.hass), self.entry.entry_id)

    def _row(self, role):
        return next((r for r in self._rows() if r.platform == DOMAIN and _role(r) == role), None)

    def _state(self, role):
        row = self._row(role)
        return self.hass.states.get(row.entity_id) if row is not None else None

    def _registry_observation(self):
        return [
            {
                "role": _role(row), "domain": row.domain, "platform": row.platform,
                "unique_id": row.unique_id, "id": row.id, "entity_id": row.entity_id,
                "name": row.name, "aliases": [alias for alias in row.aliases if isinstance(alias, str)],
            }
            for row in self._rows() if _role(row) in IDENTITY_ROLES
        ]

    def lookup_registry(self, domain, platform, unique_id):
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(domain, platform, unique_id)
        return registry.async_get(entity_id).id if entity_id is not None else None

    def _site_settings(self):
        glucose = self._state("glucose")
        if glucose is None:
            return None
        attrs = glucose.attributes
        thresholds = attrs.get("thresholds_mgdl")
        if thresholds is None:
            return None
        keys = {"urgent_high": "bg_high", "high": "bg_target_top",
                "low": "bg_target_bottom", "urgent_low": "bg_low"}
        return [{
            "display_unit": attrs.get("unit_of_measurement"), "threshold_unit": "mg/dL",
            "thresholds": {out: thresholds[key] for key, out in keys.items() if key in thresholds},
        }, None, None, "available", {}]

    async def _calendar(self, parameters):
        row = self._row("calendar")
        state = self._state("calendar")
        if state is None:
            return None
        if state.state == STATE_UNAVAILABLE:
            return [None, None, None, STATE_UNAVAILABLE, {}]
        response = await self.hass.services.async_call(
            "calendar", "get_events",
            {"entity_id": row.entity_id, "start_date_time": parameters["start"],
             "end_date_time": parameters["end"]},
            blocking=True, return_response=True,
        )
        return [[[
            event.get("uid"), _utc_iso(event.get("start")), event.get("summary"),
            event.get("insulin"), event.get("carbs"), event.get("description"),
            _utc_iso(event.get("end")),
        ] for event in response[row.entity_id]["events"]], None, None, "available", {}]

    def _sensor_observation(self, role):
        row = self._row(role)
        state = self._state(role)
        if state is None:
            return None
        attrs = state.attributes
        if role == "glucose":
            reading = self._state("last_reading")
            result = _sensor(state, row, reading.state if reading else None, ("direction", "delta"))
            if "direction" in result[4]:
                result[4]["trend"] = result[4].pop("direction")
            return result
        details = ("source_id",) if role in ("latest_bolus", "latest_carbs") else ()
        if role in MAINTENANCE_ROLES:
            details = ("event_type",)
        return _sensor(state, row, attrs.get("time"), details)

    async def observe(self, url, request):
        endpoint = urlsplit(url)
        if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port:
            raise ValueError("contract observer requires a disposable IPv4 loopback server")
        self.freezer.move_to(request["clock"])
        self.lifecycle = request["lifecycle"]
        if self.hass is None:
            await self._start(url, request)
        else:
            await self._poll(url, request)
        await self.hass.async_block_till_done()
        self.entry_state = self.entry.state.value
        operation = request["operation"]
        if operation == "maintenance":
            roles = (request["maintenance"],) if request["maintenance"] else MAINTENANCE_ROLES
        else:
            roles = OPERATIONS[operation]
        observations = {}
        for role in roles:
            if role == "registry":
                observations[role] = self._registry_observation()
            elif role == "site_settings":
                observations[role] = self._site_settings()
            elif role == "calendar":
                observations[role] = await self._calendar(request["parameters"]["calendar"])
            else:
                observations[role] = self._sensor_observation(role)
        self.observations.append((self.lifecycle, self.entry_state, observations))
        return observations

    async def close(self):
        if self.hass is None:
            return
        try:
            if self.entry is not None and self.entry.state is ConfigEntryState.LOADED:
                await self.hass.config_entries.async_unload(self.entry.entry_id)
            await self.hass.async_stop(force=True)
        finally:
            await self.context.__aexit__(None, None, None)
            self.hass = None


class FrozenRunnerObserver:
    """Bridge the synchronous frozen runner's worker to the HA event loop."""

    def __init__(self, loop, observer):
        self.loop = loop
        self.observer = observer

    def _call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def observe(self, url, request):
        return self._call(self.observer.observe(url, request))

    def close(self):
        self._call(self.observer.close())

    def load_state(self, state):
        self.observer.load_state(state)

    def dump_state(self):
        return self._call(self.observer.dump_state())

    def lookup_registry(self, domain, platform, unique_id):
        # Registry index access is a HA loop callback, independent of HTTP.
        result = Future()

        def lookup():
            try:
                result.set_result(self.observer.lookup_registry(domain, platform, unique_id))
            except Exception as error:
                result.set_exception(error)

        self.loop.call_soon_threadsafe(lookup)
        return result.result()
