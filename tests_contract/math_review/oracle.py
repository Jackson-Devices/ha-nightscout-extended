"""Exact, independent expectations for synthetic Nightscout observations."""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

type WireNumber = int | str | Decimal | Fraction
type Point = tuple[int, WireNumber]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)
_MS_PER_MINUTE = 60_000
_MS_PER_HOUR = 3_600_000


def exact(value: WireNumber) -> Fraction:
    """Keep written decimals exact and reject binary floats and booleans."""
    if isinstance(value, Fraction):
        return value
    if type(value) is int:
        return Fraction(value)
    if isinstance(value, (str, Decimal)):
        return Fraction(Decimal(value))
    raise TypeError("Expected integer or exact decimal quantity")


def utc_ms(when: datetime) -> int:
    """Get exact UTC milliseconds from an aware, millisecond-aligned instant."""
    if when.utcoffset() is None:
        raise ValueError("A UTC offset is required")
    since_epoch = when.astimezone(UTC) - _EPOCH
    millis, residual = divmod(since_epoch, _MILLISECOND)
    if residual:
        raise ValueError("Timestamp is not millisecond-aligned")
    return millis


def at_utc_ms(millis: int) -> datetime:
    """Represent an exact wire timestamp as a UTC datetime for product calls."""
    return _EPOCH + millis * _MILLISECOND


def classify_mgdl(value: WireNumber, thresholds: Mapping[str, WireNumber]) -> str:
    """Apply the bundled card's alarm intervals to a source mg/dL value.

    The lower alarms are open at their thresholds. The upper alarms start
    strictly above their thresholds. Display quantization and freshness are
    separate concerns. The caller supplies thresholds from the site.
    """
    glucose = exact(value)
    urgent_low, low, high, urgent_high = (
        exact(thresholds[key]) for key in ("urgent_low", "low", "high", "urgent_high")
    )
    if not urgent_low <= low <= high <= urgent_high:
        raise ValueError("Alarm thresholds must be in increasing order")
    intervals_crossed = sum((
        glucose >= urgent_low, glucose >= low, glucose > high, glucose > urgent_high,
    ))
    return ("urgent_low", "low", "in_range", "high", "urgent_high")[intervals_crossed]


def expected_forecast(
    anchor_ms: int | None, horizon_minutes: int, emitted: Iterable[Point],
) -> tuple[int, Fraction] | None:
    """Select an existing server prediction within the closed three-minute window.

    See the pinned integration README, Forecasts and missing values. Only
    the v2 source instant anchors the horizon. The Nightscout 15.0.6 producer
    already accounts for its +2 second offset in each emitted mills value
    (lib/plugins/ar2.js, pushPoint and ar2Point). No AR2 value is recomputed.
    Equidistant candidates have no documented policy and require adjudication.
    """
    if anchor_ms is None:
        return None
    target_ms = anchor_ms + horizon_minutes * _MS_PER_MINUTE
    tolerance_ms = 3 * _MS_PER_MINUTE
    candidates = [
        (abs(point_ms - target_ms), point_ms, exact(value))
        for point_ms, value in emitted
        if target_ms - tolerance_ms <= point_ms <= target_ms + tolerance_ms
    ]
    if not candidates:
        return None
    distance, millis, quantity = min(candidates)
    if sum(error == distance for error, _, _ in candidates) != 1:
        raise ValueError("Equidistant forecast points require an explicit policy")
    return millis, quantity


def admits_v3_current(
    observation_ms: int, value: WireNumber, *, poll_ms: int, bgnow: Point | None,
) -> bool:
    """Check the pinned README's v3 clock rule for an already usable SGV."""
    return observation_ms <= poll_ms or (
        bgnow is not None
        and observation_ms < poll_ms + _MS_PER_MINUTE
        and (observation_ms, exact(value)) == (bgnow[0], exact(bgnow[1]))
    )


def fresh_reservoir_contact(contact_ms: int, *, observed_ms: int) -> bool:
    """Accept pump-contact ages in the closed zero-to-one-hour interval."""
    return 0 <= observed_ms - contact_ms <= _MS_PER_HOUR


def maintenance_hours(event_ms: int, *, observed_ms: int) -> Fraction | None:
    """Elapsed duration since the source event on an absolute UTC timeline."""
    elapsed_ms = observed_ms - event_ms
    return Fraction(elapsed_ms, _MS_PER_HOUR) if elapsed_ms >= 0 else None
