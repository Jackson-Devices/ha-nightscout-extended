"""Polls Nightscout."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from itertools import pairwise
from math import isfinite
from time import monotonic
from typing import Literal, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NightscoutAuthError, NightscoutClient, NightscoutConnectionError, NightscoutData
from .const import (
    ALARM_THRESHOLD_OPTIONS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_THRESHOLDS_MGDL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
_STATUS_REFRESH_SECONDS = 300
_THRESHOLD_FIELDS = {
    "urgent_low": "bgLow",
    "low": "bgTargetBottom",
    "high": "bgTargetTop",
    "urgent_high": "bgHigh",
}
_ALARM_FLAG_FIELDS = {
    "urgent_low": "alarmUrgentLow",
    "low": "alarmLow",
    "high": "alarmHigh",
    "urgent_high": "alarmUrgentHigh",
}

type NightscoutConfigEntry = ConfigEntry[NightscoutCoordinator]


class AlarmSettings(TypedDict):
    """Settings for the HA threshold indication."""

    units: Literal["mg/dl", "mmol"]
    thresholds_mgdl: dict[str, float]
    enabled: dict[str, bool]


def finite_threshold_mgdl(value: object) -> float | None:
    """Accept only finite numeric values for alarm thresholds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if isfinite(number) else None


def thresholds_ordered(thresholds: Mapping[str, float]) -> bool:
    """Check known thresholds in Nightscout's raw mg/dL order."""
    values = (thresholds[key] for key in ALARM_THRESHOLD_OPTIONS if key in thresholds)
    return all(lower < upper for lower, upper in pairwise(values))


class NightscoutCoordinator(DataUpdateCoordinator[NightscoutData]):
    """Fetches all values in one request."""

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
        self._status_refresh_due = monotonic() + _STATUS_REFRESH_SECONDS
        self._alarm_status_available = True

    @property
    def _site_settings(self) -> dict:
        settings = self.status.get("settings")
        return settings if isinstance(settings, dict) else {}

    @property
    def site_alarm_thresholds_mgdl(self) -> dict[str, float]:
        """Read known thresholds for options validation without chart defaults."""
        raw = self._site_settings.get("thresholds")
        if not self._alarm_status_available or not isinstance(raw, dict):
            return {}
        return {
            key: number
            for key, field in _THRESHOLD_FIELDS.items()
            if (number := finite_threshold_mgdl(raw.get(field))) is not None
        }

    @property
    def alarm_settings(self) -> AlarmSettings | None:
        """Read current site units and enable flags, with local threshold overrides."""
        settings = self._site_settings
        units = settings.get("units")
        if not self._alarm_status_available or units not in ("mg/dl", "mmol"):
            return None

        enabled = {}
        for key, field in _ALARM_FLAG_FIELDS.items():
            flag = settings.get(field)
            if not isinstance(flag, bool):
                return None
            enabled[key] = flag

        thresholds = self.site_alarm_thresholds_mgdl
        for key, option in ALARM_THRESHOLD_OPTIONS.items():
            if option in self.config_entry.options:
                value = finite_threshold_mgdl(self.config_entry.options[option])
                if value is None or value <= 0:
                    return None
                thresholds[key] = value

        if len(thresholds) != len(ALARM_THRESHOLD_OPTIONS) or not thresholds_ordered(thresholds):
            return None
        return {"units": units, "thresholds_mgdl": thresholds, "enabled": enabled}

    @property
    def thresholds_mgdl(self) -> dict[str, float]:
        """The site's alarm thresholds (BG_LOW, BG_TARGET_BOTTOM/TOP, BG_HIGH) in mg/dL.

        Nightscout reports them in mg/dL whatever the display unit; its defaults fill gaps.
        """
        raw = self._site_settings.get("thresholds")
        out = dict(DEFAULT_THRESHOLDS_MGDL)
        if isinstance(raw, dict):
            for key, field in _THRESHOLD_FIELDS.items():
                try:
                    value = float(raw[field])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if isfinite(value) and value > 0:
                    out[key] = value
        return out

    async def _async_refresh_status(self) -> None:
        if monotonic() < self._status_refresh_due:
            return
        uses_api_secret = self.client.uses_api_secret
        try:
            self.status = await self.client.async_get_status()
        except (NightscoutAuthError, NightscoutConnectionError):
            # A failed status-only read must not invalidate healthy readings or chart bands.
            self.client.set_uses_api_secret(uses_api_secret)
            self._alarm_status_available = False
        else:
            self._alarm_status_available = True
        finally:
            self._status_refresh_due = monotonic() + _STATUS_REFRESH_SECONDS

    async def _async_update_data(self) -> NightscoutData:
        try:
            data = await self.client.async_get_data()
        except NightscoutAuthError as err:
            raise ConfigEntryAuthFailed("Nightscout rejected the token or API secret") from err
        except NightscoutConnectionError as err:
            raise UpdateFailed(f"Could not read Nightscout: {err}") from err
        await self._async_refresh_status()
        return data
