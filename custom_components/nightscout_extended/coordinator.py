"""Polls Nightscout."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta
from functools import partial

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .age import AGE_EVENT_TYPES
from .api import (
    NightscoutAuthError,
    NightscoutClient,
    NightscoutConnectionError,
    NightscoutData,
    ReservoirReading,
    _apply_v3_glucose,
)
from .const import (
    CONF_RESERVOIR_UPLOADER,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_THRESHOLDS_MGDL,
    DOMAIN,
)
from .sgv_history import SGVHistory

_LOGGER = logging.getLogger(__name__)
_READ_ERRORS = (NightscoutAuthError, NightscoutConnectionError)
_TIMEAGO_LIMITS = (("Warn", 15), ("Urgent", 30))

type NightscoutConfigEntry = ConfigEntry[NightscoutCoordinator]


def reservoir_binding_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, str]]:
    """Keep the one-time uploader selection private and tied to the config entry."""
    return Store(hass, 1, f"{DOMAIN}.reservoir.{entry_id}", private=True, atomic_writes=True)


def _timeago_lifetime(settings: dict) -> timedelta | None:
    """Apply the first enabled Nightscout timeago limit in whole milliseconds."""
    limits = []
    for alert, default in _TIMEAGO_LIMITS:
        key = f"alarmTimeago{alert}"
        enabled = settings.get(key, True)
        if isinstance(enabled, str) and enabled.lower() in {"off", "false"}:
            continue
        if not enabled:
            continue
        minutes = settings.get(f"{key}Mins") or default
        if isinstance(minutes, str):
            minutes = minutes.strip() or "0"
        try:
            # The stock comparison is strictly greater, and its clock is in ms.
            expired_ms = math.floor(float(minutes) * 60 * 1000) + 1
            limits.append(timedelta(milliseconds=expired_ms))
        except (TypeError, ValueError, OverflowError):
            continue
    return min(limits, default=None)


def _source_expiry(source: datetime | None, lifetime: timedelta | None) -> datetime | None:
    if source is None or lifetime is None:
        return None
    try:
        return source + lifetime
    except OverflowError:
        return datetime.min.replace(tzinfo=UTC) if lifetime < timedelta(0) else None


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
        self._freshness_deadlines: dict[str, datetime] = {}
        self._expired_signals: set[str] = set()
        self._freshness_listeners: dict[str, set[CALLBACK_TYPE]] = {}
        self._unsub_freshness: CALLBACK_TYPE | None = None
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

    def signal_expired(self, key: str) -> bool:
        """Return the last published freshness state for a signal."""
        return key in self._expired_signals

    @callback
    def async_add_freshness_listener(self, key: str, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Subscribe a sensor to local expiry without affecting the poll cadence."""
        listeners = self._freshness_listeners.setdefault(key, set())
        listeners.add(listener)
        return partial(listeners.discard, listener)

    @callback
    def _async_refresh_finished(self) -> None:
        """Recompute deadlines for the newly published poll before entity updates."""
        super()._async_refresh_finished()
        self._async_cancel_freshness()
        if not self.last_update_success or self._shutdown_requested or self.hass.is_stopping:
            return

        data = self.data
        lifetime = _timeago_lifetime(self.status.get("settings") or {})
        deadlines = {}
        glucose_expiry = _source_expiry(data.glucose_time, lifetime)
        if self.glucose_available and glucose_expiry is not None:
            deadlines["glucose"] = glucose_expiry
        if self.calculations_available:
            forecast_expiry = _source_expiry(data.forecast_source_time, lifetime)
            for minutes in (15, 30):
                if (point := data.forecast_at(minutes)) is not None:
                    deadlines[f"forecast_{minutes}"] = (
                        point.time if forecast_expiry is None else min(point.time, forecast_expiry)
                    )
        self._freshness_deadlines = deadlines
        self._async_schedule_freshness()

    @callback
    def _async_schedule_freshness(self) -> None:
        now = dt_util.utcnow()
        deadlines = self._freshness_deadlines
        self._expired_signals = {key for key, deadline in deadlines.items() if deadline <= now}
        next_deadline = min((deadline for deadline in deadlines.values() if deadline > now), default=None)
        if next_deadline is not None:
            self._unsub_freshness = async_track_point_in_utc_time(
                self.hass, self._async_freshness_changed, next_deadline,
            )

    @callback
    def _async_freshness_changed(self, _scheduled_at: datetime) -> None:
        self._unsub_freshness = None
        if self._shutdown_requested or self.hass.is_stopping:
            return

        previous = self._expired_signals
        self._async_schedule_freshness()
        for key in previous ^ self._expired_signals:
            for listener in tuple(self._freshness_listeners.get(key, ())):
                try:
                    listener()
                except Exception:
                    _LOGGER.exception("Unexpected error updating Nightscout freshness for %s", key)

    @callback
    def _async_cancel_freshness(self) -> None:
        if self._unsub_freshness is not None:
            self._unsub_freshness()
            self._unsub_freshness = None

    async def async_shutdown(self) -> None:
        self._async_cancel_freshness()
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

        calculations_available = isinstance(properties, NightscoutData)
        raw_available = not isinstance(history, BaseException)

        data = properties if isinstance(properties, NightscoutData) else NightscoutData()
        if self.uses_v3 or not calculations_available:
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
        try:
            data.reservoir = await self._async_get_reservoir(now)
        except (NightscoutAuthError, NightscoutConnectionError, HomeAssistantError, OSError):
            # A device-status permission, response or storage failure is local to the reservoir.
            data.reservoir = None

        # Commit availability with the data HA will publish, after all awaits.
        self.calculations_available = calculations_available
        self.treatments_available = isinstance(treatments, list)
        self.glucose_available = raw_available or (not self.uses_v3 and calculations_available)
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
