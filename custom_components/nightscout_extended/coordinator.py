"""Polls Nightscout."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import NightscoutAuthError, NightscoutClient, NightscoutConnectionError, NightscoutData, ReservoirReading
from .const import CONF_RESERVOIR_UPLOADER, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DEFAULT_THRESHOLDS_MGDL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type NightscoutConfigEntry = ConfigEntry[NightscoutCoordinator]


def reservoir_binding_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, str]]:
    """Keep the one-time uploader selection private and tied to the config entry."""
    return Store(hass, 1, f"{DOMAIN}.reservoir.{entry_id}", private=True, atomic_writes=True)


class NightscoutCoordinator(DataUpdateCoordinator[NightscoutData]):
    """Poll Nightscout for the site's entities."""

    config_entry: NightscoutConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: NightscoutConfigEntry, client: NightscoutClient, units: str
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        self.units = units
        self.status: dict = {}
        self._reservoir_binding = reservoir_binding_store(hass, entry.entry_id)
        self._reservoir_uploader: str | None = None

    @property
    def thresholds_mgdl(self) -> dict[str, float]:
        """The site's alarm thresholds (BG_LOW, BG_TARGET_BOTTOM/TOP, BG_HIGH) in mg/dL.

        Nightscout reports them in mg/dL whatever the display unit; its defaults fill gaps.
        """
        raw = (self.status.get("settings") or {}).get("thresholds") or {}
        out = dict(DEFAULT_THRESHOLDS_MGDL)
        for key, ns_key in (
            ("urgent_low", "bgLow"),
            ("low", "bgTargetBottom"),
            ("high", "bgTargetTop"),
            ("urgent_high", "bgHigh"),
        ):
            try:
                value = float(raw[ns_key])
            except (KeyError, TypeError, ValueError):
                continue
            if value > 0:
                out[key] = value
        return out

    async def _async_update_data(self) -> NightscoutData:
        poll_time = dt_util.utcnow()
        try:
            data = await self.client.async_get_data()
        except NightscoutAuthError as err:
            raise ConfigEntryAuthFailed("Nightscout rejected the token or API secret") from err
        except NightscoutConnectionError as err:
            raise UpdateFailed(f"Could not read Nightscout: {err}") from err

        try:
            data.reservoir = await self._async_get_reservoir(poll_time)
        except (NightscoutAuthError, NightscoutConnectionError, HomeAssistantError, OSError):
            # A device-status permission, response or storage failure is local to the reservoir.
            data.reservoir = None
        return data

    async def _async_get_reservoir(self, poll_time: datetime) -> ReservoirReading | None:
        explicit = self.config_entry.options.get(CONF_RESERVOIR_UPLOADER)
        if explicit:
            return await self.client.async_get_reservoir(poll_time, explicit)

        if self._reservoir_uploader is None:
            saved = await self._reservoir_binding.async_load()
            if saved is not None:
                device = saved.get("device") if isinstance(saved, dict) else None
                if not isinstance(device, str) or not device.strip():
                    return None
                self._reservoir_uploader = device

        reading = await self.client.async_get_reservoir(poll_time, self._reservoir_uploader)
        if reading is None or self._reservoir_uploader is not None:
            return reading
        if self.hass.is_stopping:
            return None

        # Store handles certain write errors internally. Read back a new Store
        # instance so an in-memory pending write cannot enable automatic selection.
        binding = {"device": reading.uploader}
        await self._reservoir_binding.async_save(binding)
        if await reservoir_binding_store(self.hass, self.config_entry.entry_id).async_load() != binding:
            return None
        self._reservoir_uploader = reading.uploader
        return reading
