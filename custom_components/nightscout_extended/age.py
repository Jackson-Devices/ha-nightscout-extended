"""Definitions shared by treatment age lookups and sensors."""

from __future__ import annotations

from dataclasses import dataclass
from operator import attrgetter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import AgeMarker


@dataclass(frozen=True)
class AgeSignal:
    key: str
    event_types: tuple[str, ...]

    def marker(self, markers: dict[str, AgeMarker] | None) -> AgeMarker | None:
        """Pick the newest supported event; later event types win equal-time ties."""
        if not markers:
            return None
        return max(
            (markers[kind] for kind in reversed(self.event_types) if kind in markers),
            key=attrgetter("time"),
            default=None,
        )


AGE_SIGNALS = (
    AgeSignal("insulin_age", ("Insulin Change",)),
)

AGE_EVENT_TYPES = tuple(dict.fromkeys(kind for signal in AGE_SIGNALS for kind in signal.event_types))
