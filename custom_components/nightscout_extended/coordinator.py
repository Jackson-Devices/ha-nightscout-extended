"""Polls Nightscout."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .age import AGE_EVENT_TYPES
from .api import (
    NightscoutAuthError,
    NightscoutClient,
    NightscoutConnectionError,
    NightscoutData,
    _apply_v3_glucose,
)
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DEFAULT_THRESHOLDS_MGDL, DOMAIN
from .sgv_history import SGVHistory

_LOGGER = logging.getLogger(__name__)
_READ_ERRORS = (NightscoutAuthError, NightscoutConnectionError)

type NightscoutConfigEntry = ConfigEntry[NightscoutCoordinator]


class NightscoutCoordinator(DataUpdateCoordinator[NightscoutData]):
    """Refresh the independent Nightscout data sources for one site."""

    config_entry: NightscoutConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: NightscoutConfigEntry, client: NightscoutClient, units: str,
        history: SGVHistory, *, uses_v3: bool,
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
        self.history = history
        self.uses_v3 = uses_v3
        self.calculations_available = False
        self.glucose_available = False
        self.treatments_available = False
        self._failed_groups: set[str] = set()
        self._poll_lock = asyncio.Lock()
        self.units = units
        self.status: dict = {}

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

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        # Wait for an active refresh to leave the store writer before unloading.
        async with self._poll_lock:
            pass

    async def _async_update_data(self) -> NightscoutData:
        async with self._poll_lock:
            return await self._async_poll()

    async def _async_poll(self) -> NightscoutData:
        now = dt_util.utcnow()
        properties, treatments, history, age_markers = await asyncio.gather(
            self.client.async_get_data(calculated_only=True),
            self.client.async_get_treatments(now - timedelta(hours=48), now + timedelta(days=62)),
            self.history.async_refresh(now),
            self.client.async_get_age_markers(AGE_EVENT_TYPES, now),
            return_exceptions=True,
        )
        failed_groups: set[str] = set()
        for name, result in (
            ("calculated values", properties), ("treatments", treatments), ("SGV history", history),
            ("age markers", age_markers),
        ):
            if not isinstance(result, BaseException):
                continue
            if not isinstance(result, _READ_ERRORS):
                raise result
            failed_groups.add(name)
            if (
                isinstance(result, NightscoutAuthError)
                and name != "age markers"
                and (name != "SGV history" or self.uses_v3)
                and (
                    result.status != 403
                    or self.config_entry.state is ConfigEntryState.SETUP_IN_PROGRESS
                )
            ):
                raise ConfigEntryAuthFailed("Nightscout rejected the token or API secret") from result

        for name in failed_groups - self._failed_groups:
            _LOGGER.warning("Nightscout %s could not be refreshed", name)
        self._failed_groups = failed_groups
        if len(failed_groups) == 4:
            raise UpdateFailed("No Nightscout data source responded successfully")

        self.calculations_available = isinstance(properties, NightscoutData)
        self.treatments_available = isinstance(treatments, list)
        raw_available = not isinstance(history, BaseException)
        self.glucose_available = raw_available or (
            not self.uses_v3 and self.calculations_available
        )

        data = properties if isinstance(properties, NightscoutData) else NightscoutData()
        if self.uses_v3 or not self.calculations_available:
            sample = (
                self.history.select_current(now, data.glucose_time, data.glucose)
                if raw_available
                else None
            )
            _apply_v3_glucose(data, sample)

        if isinstance(treatments, list):
            data.treatments = treatments
        elif self.data is not None:
            # Retain the previous successful range for the calendar's independent fallback.
            data.treatments = self.data.treatments
        data.age_markers = age_markers if isinstance(age_markers, dict) else None
        return data
