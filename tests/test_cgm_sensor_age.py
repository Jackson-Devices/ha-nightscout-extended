"""Tests for selecting the CGM age marker, without Home Assistant."""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_api import api

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location(
    "ns_cgm_age", ROOT / "custom_components" / "nightscout_extended" / "age.py"
)
age = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = age
spec.loader.exec_module(age)


@pytest.mark.parametrize(
    ("start_offset", "change_offset", "expected_type"),
    [
        (1, 0, "Sensor Start"),
        (0, 1, "Sensor Change"),
        (0, 0, "Sensor Change"),
    ],
)
@pytest.mark.parametrize("reverse_order", [False, True])
def test_cgm_age_prefers_newest_marker_and_change_wins_ties(
    start_offset, change_offset, expected_type, reverse_order,
):
    origin = datetime(2026, 1, 12, tzinfo=timezone.utc)
    markers = {
        "Sensor Start": api.AgeMarker(origin + timedelta(hours=start_offset), "Sensor Start"),
        "Sensor Change": api.AgeMarker(origin + timedelta(hours=change_offset), "Sensor Change"),
        "Insulin Change": api.AgeMarker(origin + timedelta(hours=2), "Insulin Change"),
    }
    expected = markers[expected_type]
    if reverse_order:
        markers = dict(reversed(markers.items()))

    signal = next(signal for signal in age.AGE_SIGNALS if signal.key == "sensor_age")
    assert signal.marker(markers) == expected
