"""Nightscout Extended: glucose, insulin on board, carbs on board and forecast from Nightscout."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_URL, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, valid_entity_id
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import NightscoutAuthError, NightscoutClient, NightscoutConnectionError
from .card import async_register_card
from .const import (
    CONF_SGV_HISTORY_DAYS,
    CONF_USE_API_SECRET,
    DOMAIN,
    SERVICE_GET_GLUCOSE_HISTORY,
    UNIT_MGDL,
    UNIT_MMOL,
)
from .coordinator import NightscoutConfigEntry, NightscoutCoordinator
from .sgv_history import DEFAULT_HISTORY_DAYS, SGVHistory
from .sgv_source import NightscoutSGVSource

PLATFORMS = [Platform.CALENDAR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-wide actions and make the glucose card available."""
    async def get_glucose_history(call: ServiceCall) -> ServiceResponse:
        return await _async_get_glucose_history(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_GET_GLUCOSE_HISTORY, get_glucose_history,
        supports_response=SupportsResponse.ONLY,
    )
    await async_register_card(hass)
    return True


def _create_client(hass: HomeAssistant, entry: NightscoutConfigEntry) -> NightscoutClient:
    client = NightscoutClient(
        async_get_clientsession(hass), entry.data[CONF_URL], entry.data.get(CONF_API_KEY)
    )
    client.set_uses_api_secret(entry.data.get(CONF_USE_API_SECRET, False))
    return client


def _create_history(
    hass: HomeAssistant, entry: NightscoutConfigEntry, source: NightscoutSGVSource,
) -> SGVHistory:
    return SGVHistory(
        hass, entry.entry_id, source,
        entry.options.get(CONF_SGV_HISTORY_DAYS, DEFAULT_HISTORY_DAYS),
    )


def _range_time(value: object) -> datetime:
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if isinstance(value, datetime) and value.utcoffset() is not None:
            return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        pass
    raise ServiceValidationError("Glucose history times must include a UTC offset.")


async def _async_get_glucose_history(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    if set(call.data) != {"entity_id", "start", "end"}:
        raise ServiceValidationError("Specify one glucose entity, a start time, and an end time.")
    entity_id = call.data["entity_id"]
    if not isinstance(entity_id, str) or not valid_entity_id(entity_id):
        raise ServiceValidationError("Specify one Nightscout Extended glucose sensor.")

    # Websocket calls supply the authenticated user in the native service context.
    if (user_id := call.context.user_id) is not None:
        user = await hass.auth.async_get_user(user_id)
        if user is None or not user.is_active or not user.permissions.check_entity(entity_id, POLICY_READ):
            raise Unauthorized(context=call.context, entity_id=entity_id, permission=POLICY_READ)

    registered = er.async_get(hass).async_get(entity_id)
    if (
        registered is None
        or registered.platform != DOMAIN
        or registered.domain != Platform.SENSOR
        or registered.disabled_by is not None
        or registered.config_entry_id is None
    ):
        raise ServiceValidationError("Specify one Nightscout Extended glucose sensor.")
    candidate = hass.config_entries.async_get_entry(registered.config_entry_id)
    if (
        candidate is None
        or candidate.domain != DOMAIN
        or candidate.disabled_by is not None
        or registered.unique_id != glucose_unique_id(candidate)
    ):
        raise ServiceValidationError("Specify one Nightscout Extended glucose sensor.")
    entry = cast(NightscoutConfigEntry, candidate)

    start, end = _range_time(call.data["start"]), _range_time(call.data["end"])
    if end <= start:
        raise ServiceValidationError("The glucose history end must be after the start.")
    if entry.state is ConfigEntryState.LOADED:
        history = entry.runtime_data.history
        allow_remote = True
    elif entry.state is ConfigEntryState.SETUP_RETRY:
        # A previous setup may have committed data even while the site is offline.
        source = NightscoutSGVSource(_create_client(hass, entry))
        history = _create_history(hass, entry, source)
        allow_remote = False
    else:
        raise ServiceValidationError("The Nightscout Extended entry is unavailable.")
    try:
        return await history.async_range(start, end, allow_remote=allow_remote)
    except (ValueError, OverflowError, OSError) as err:
        raise ServiceValidationError("The glucose history range is invalid.") from err


def glucose_unique_id(entry: NightscoutConfigEntry) -> str:
    """Return the existing registry identity of an entry's primary glucose sensor."""
    return f"{entry.unique_id or entry.entry_id}_glucose"


async def async_setup_entry(hass: HomeAssistant, entry: NightscoutConfigEntry) -> bool:
    client = _create_client(hass, entry)
    try:
        status = await client.async_get_status()
    except NightscoutAuthError as err:
        raise ConfigEntryAuthFailed("Nightscout rejected the token or API secret") from err
    except NightscoutConnectionError as err:
        raise ConfigEntryNotReady("Could not reach Nightscout") from err

    source = NightscoutSGVSource(client)
    history = _create_history(hass, entry, source)
    units = (status.get("settings") or {}).get("units", "mg/dl")
    coordinator = NightscoutCoordinator(
        hass, entry, client, UNIT_MMOL if str(units).lower().startswith("mmol") else UNIT_MGDL,
        history, uses_v3=source.uses_v3,
    )
    coordinator.status = status
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: NightscoutConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: NightscoutConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_shutdown()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: NightscoutConfigEntry) -> None:
    """Delete the owned private cache only when removing its config entry."""
    source = NightscoutSGVSource(_create_client(hass, entry))
    await _create_history(hass, entry, source).async_remove()
