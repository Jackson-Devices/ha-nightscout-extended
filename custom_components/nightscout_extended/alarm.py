"""Read-only threshold indication following Nightscout simple alarms."""

from datetime import UTC, datetime, timedelta
from math import floor, isfinite
from typing import Any

_FRESHNESS = timedelta(minutes=10)
_MMOL_TO_MGDL = 18.01559  # Nightscout's conversion factor for simple alarms.


def _scale_mgdl(value: float, units: str) -> float:
    if units == "mmol":
        scaled = value / _MMOL_TO_MGDL * 10
        whole = floor(scaled)
        # JavaScript Math.round rounds ties toward positive infinity.
        return (whole + (scaled - whole >= 0.5)) / 10
    return value


def evaluate_simple_alarm(
    mgdl: float | None,
    source_time: datetime | None,
    settings: dict[str, Any] | None,
    now: datetime,
) -> tuple[str | None, datetime | None]:
    """Return the current threshold indication and its UTC freshness deadline."""
    if (
        settings is None
        or mgdl is None
        or not isfinite(mgdl)
        or mgdl <= 39
        or source_time is None
        or source_time > now
        or now - source_time >= _FRESHNESS
    ):
        return None, None

    units = settings["units"]
    thresholds = settings["thresholds_mgdl"]
    enabled = settings["enabled"]
    glucose = _scale_mgdl(mgdl, units)
    alarm = "none"
    if enabled["urgent_high"] and glucose > _scale_mgdl(thresholds["urgent_high"], units):
        alarm = "urgent_high"
    elif enabled["high"] and glucose > _scale_mgdl(thresholds["high"], units):
        alarm = "high"

    # Stock makes a second, independent low-side comparison.
    if enabled["urgent_low"] and glucose < _scale_mgdl(thresholds["urgent_low"], units):
        alarm = "urgent_low"
    elif enabled["low"] and glucose < _scale_mgdl(thresholds["low"], units):
        alarm = "low"
    return alarm, source_time.astimezone(UTC) + _FRESHNESS
