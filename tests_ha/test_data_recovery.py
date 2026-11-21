"""Follow missing and late Nightscout data through a loaded Home Assistant entry."""

from datetime import timedelta
import re
from time import time

import pytest
from aiohttp import ClientConnectionError
from homeassistant import config_entries
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from test_v3_integration import (
    BEARER,
    CALENDAR,
    ENTRIES_V1,
    ENTRIES_V3,
    EXCHANGE,
    HISTORY_V3,
    LAST_MODIFIED,
    PROPERTIES,
    READING,
    TOKEN,
    TREATMENTS_V1,
    TREATMENTS_V3,
    URL,
    VERSION,
    _assert_wire_mode,
    _configure,
    _mock_v1_sgv,
    _mock_v3_sgv,
    _ms,
    _refresh,
    _state,
)

NEW_READING = "2026-01-08T12:38:00+00:00"
LATE_READING = "2026-01-08T12:18:00+00:00"
LATE_TREATMENT = "2026-01-08T12:12:00+00:00"
FIRST_FORECAST = (("2026-01-08T12:15:02+00:00", 207), ("2026-01-08T12:30:02+00:00", 231))
RECOMPUTED_FORECAST = (("2026-01-08T12:53:02+00:00", 179), ("2026-01-08T13:08:02+00:00", 192))


def _sgv(identifier, measured, glucose, written):
    return {
        "identifier": identifier,
        "type": "sgv",
        "date": _ms(measured),
        "sgv": glucose,
        "srvModified": _ms(written),
    }


FIRST_SGV = _sgv("first-sgv", READING, 153, "2026-01-08T12:00:01+00:00")
NEW_SGV = _sgv("new-sgv", NEW_READING, 166, "2026-01-08T12:40:58+00:00")
LATE_SGV = _sgv("late-sgv", LATE_READING, 93, "2026-01-08T12:41:30+00:00")


def _curve(*points):
    return {
        "forecast": {
            "predicted": [
                {"mills": _ms(point[0]), "mgdl": point[1]} if isinstance(point, tuple) else point
                for point in points
            ],
        },
    }


def _snapshot(
    mocker: AiohttpClientMocker,
    *,
    v3: bool,
    reading=(READING, 153),
    entries=(FIRST_SGV,),
    deleted=(),
    ar2=None,
    late=False,
):
    """Expose v2 calculations and the site's separate stored collections."""
    mocker.clear_requests()
    bgnow = {"last": reading[1], "mills": _ms(reading[0])} if reading else {}
    props = {
        "bgnow": bgnow,
        "iob": {"iob": 1.35, "source": "fixture-pump"},
        "cob": {"cob": 28.9, "source": "fixture-meal"},
    }
    if ar2 is not None:
        props["ar2"] = ar2

    mocker.get(
        f"{URL}/api/v1/status.json",
        json={"version": "15.0.9", "settings": {"customTitle": "Parity Site", "units": "mg/dl"}},
    )
    mocker.get(f"{URL}/api/v2/properties/bgnow", json={"bgnow": bgnow})
    mocker.get(f"{URL}{PROPERTIES}", json=props)

    treatments = [
        {"_id": "late-carb", "eventType": "Carb Correction", "carbs": 18, "created_at": LATE_TREATMENT}
    ] if late else []
    if not v3:
        mocker.get(f"{URL}{VERSION}", status=404)
        legacy_entries = []
        for row in entries:
            legacy_row = row.copy()
            legacy_row["_id"] = legacy_row.pop("identifier")
            legacy_entries.append(legacy_row)
        _mock_v1_sgv(mocker, legacy_entries)
        mocker.get(f"{URL}{TREATMENTS_V1}", params={"sort[0][0]": "duration"}, json=[])
        mocker.get(f"{URL}{TREATMENTS_V1}", json=treatments)
        return

    now = int(time())
    mocker.get(
        f"{URL}{VERSION}",
        json={"status": 200, "result": {"version": "15.0.9", "apiVersion": "3.0.5"}},
    )
    mocker.get(f"{URL}{EXCHANGE}", json={"token": BEARER, "iat": now, "exp": now + 86400})
    _mock_v3_sgv(mocker, entries, deleted=deleted)

    # The same late document can match both the date and created_at scans.
    v3_treatments = [
        {**row, "identifier": "late-carb-v3", "date": _ms(row["created_at"])} for row in treatments
    ]
    mocker.get(f"{URL}{TREATMENTS_V3}", params={"sort$desc": "duration"}, json={"status": 200, "result": []})
    mocker.get(f"{URL}{TREATMENTS_V3}", json={"status": 200, "result": v3_treatments})


def _assert_reading_and_calculations(hass, glucose, measured_at):
    assert _state(hass, "glucose").state == glucose
    assert _state(hass, "last_reading").state == measured_at
    assert _state(hass, "insulin_on_board").state == "1.35"
    assert _state(hass, "insulin_on_board").attributes["source"] == "fixture-pump"
    assert _state(hass, "carbs_on_board").state == "28.9"
    assert _state(hass, "carbs_on_board").attributes["source"] == "fixture-meal"


def _assert_forecasts(hass, source_time, first, second):
    for horizon, point in ((15, first), (30, second)):
        state = _state(hass, f"forecast_in_{horizon}_minutes")
        assert state.attributes["source_time"] == source_time
        if point is None:
            assert state.state == STATE_UNKNOWN
            assert state.attributes["forecast_time"] is None
        else:
            when, glucose = point
            assert state.state == str(glucose)
            assert state.attributes["forecast_time"] == when


async def _calendar_range(hass):
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": CALENDAR,
            "start_date_time": "2026-01-08T12:10:00+00:00",
            "end_date_time": "2026-01-08T12:20:00+00:00",
        },
        blocking=True,
        return_response=True,
    )
    return [(item["summary"], item["start"], item["end"]) for item in result[CALENDAR]["events"]]


@pytest.mark.parametrize("v3", [False, True], ids=["legacy", "v3"])
async def test_same_entry_recovers_from_missing_data_outage_and_late_uploads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, v3,
):
    """Expose only readings and forecasts the site actually reports, with their original times."""
    freezer.move_to("2026-01-08T12:02:00+00:00")
    base_curve = _curve(*FIRST_FORECAST)
    _snapshot(aioclient_mock, v3=v3, ar2=base_curve)
    entry = await _configure(hass)
    coordinator = entry.runtime_data
    try:
        _assert_wire_mode(aioclient_mock, v3=v3, token=TOKEN, discovery=True, bootstrap=v3)
        _assert_reading_and_calculations(hass, "153", READING)
        _assert_forecasts(hass, READING, *FIRST_FORECAST)
        assert await _calendar_range(hass) == []

        # A repeated reading and curve retain their source times after both horizons pass.
        freezer.move_to("2026-01-08T12:35:00+00:00")
        await _refresh(hass, entry)
        _assert_reading_and_calculations(hass, "153", READING)
        _assert_forecasts(hass, READING, *FIRST_FORECAST)

        for curve, expected in (
            (_curve(FIRST_FORECAST[0], ("2026-01-08T12:34:01+00:00", 249)), (FIRST_FORECAST[0], None)),
            (_curve(None, FIRST_FORECAST[1]), (None, FIRST_FORECAST[1])),
            (_curve(FIRST_FORECAST[0], {"mills": "malformed", "mgdl": 999}, FIRST_FORECAST[1]), FIRST_FORECAST),
            (None, (None, None)),
        ):
            _snapshot(aioclient_mock, v3=v3, ar2=curve)
            await _refresh(hass, entry)
            _assert_reading_and_calculations(hass, "153", READING)
            _assert_forecasts(hass, READING, *expected)

        # Nothing was written since the full refresh at 12:35. Two no-change
        # ticks retain the known sample even though Nightscout has no HISTORY rows.
        _snapshot(aioclient_mock, v3=v3)
        for _ in range(2):
            aioclient_mock.mock_calls.clear()
            await _refresh(hass, entry)
            _assert_reading_and_calculations(hass, "153", READING)
            _assert_forecasts(hass, READING, None, None)
            if v3:
                series_calls = [
                    url.path for _, url, _, _ in aioclient_mock.mock_calls
                    if url.path in {ENTRIES_V3, LAST_MODIFIED} or url.path.startswith(HISTORY_V3)
                ]
                assert len(series_calls) == 1 and series_calls[0].startswith(HISTORY_V3)
                assert int(series_calls[0].removeprefix(HISTORY_V3)) >= FIRST_SGV["srvModified"]

        # A versioned tombstone, or an empty authoritative v1 snapshot and
        # empty v2 bgnow, is the explicit removal. The V3 v2 anchor remains old.
        freezer.move_to("2026-01-08T12:35:01+00:00")
        removed = {
            "identifier": FIRST_SGV["identifier"], "type": "sgv", "isValid": False,
            "srvModified": _ms("2026-01-08T12:35:01+00:00"),
        }
        _snapshot(
            aioclient_mock, v3=v3, reading=(READING, 153) if v3 else None,
            entries=(), deleted=(removed,) if v3 else (),
        )
        await _refresh(hass, entry)
        _assert_reading_and_calculations(hass, STATE_UNKNOWN, STATE_UNKNOWN)
        _assert_forecasts(hass, READING if v3 else None, None, None)

        aioclient_mock.clear_requests()
        aioclient_mock.get(
            re.compile(rf"^{re.escape(URL)}/api/"),
            exc=ClientConnectionError("synthetic site unreachable"),
        )
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert entry.state is config_entries.ConfigEntryState.LOADED
        assert not coordinator.last_update_success
        outage_paths = {url.path for _, url, _, _ in aioclient_mock.mock_calls}
        if v3:
            raw_paths = {path for path in outage_paths if path.startswith(HISTORY_V3)}
            assert len(raw_paths) == 1
            assert outage_paths - raw_paths == {PROPERTIES, TREATMENTS_V3}
        else:
            assert outage_paths == {PROPERTIES, TREATMENTS_V1, ENTRIES_V1}
        for name in (
            "glucose", "last_reading", "insulin_on_board", "carbs_on_board",
            "forecast_in_15_minutes", "forecast_in_30_minutes",
        ):
            assert _state(hass, name).state == STATE_UNAVAILABLE
        assert hass.states.get(CALENDAR).state == STATE_UNAVAILABLE

        # Recovery is triggered by HA's next scheduled poll on the same entry.
        restored = {**FIRST_SGV, "srvModified": removed["srvModified"] + 1}
        _snapshot(aioclient_mock, v3=v3, entries=(restored,), ar2=base_curve)
        freezer.tick(coordinator.update_interval + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.last_update_success
        assert entry.runtime_data is coordinator
        _assert_wire_mode(aioclient_mock, v3=v3, token=TOKEN)
        _assert_reading_and_calculations(hass, "153", READING)
        _assert_forecasts(hass, READING, *FIRST_FORECAST)

        freezer.move_to("2026-01-08T12:41:00+00:00")
        latest = (NEW_READING, 166)
        short_curve = _curve(("2026-01-08T12:43:02+00:00", 170))
        _snapshot(aioclient_mock, v3=v3, reading=latest, entries=(restored, NEW_SGV), ar2=short_curve)
        await _refresh(hass, entry)
        _assert_reading_and_calculations(hass, "166", NEW_READING)
        _assert_forecasts(hass, NEW_READING, None, None)

        freezer.move_to("2026-01-08T12:42:00+00:00")
        retained = (restored, NEW_SGV, LATE_SGV)
        _snapshot(aioclient_mock, v3=v3, reading=latest, entries=retained, ar2=short_curve, late=True)
        await _refresh(hass, entry)
        _assert_reading_and_calculations(hass, "166", NEW_READING)
        _assert_forecasts(hass, NEW_READING, None, None)
        new_event = [("Carb Correction: 18 g carbs", LATE_TREATMENT, "2026-01-08T12:17:00+00:00")]
        assert await _calendar_range(hass) == new_event
        assert _state(hass, "last_carbs").state == "18"
        assert _state(hass, "last_carbs").attributes["time"] == LATE_TREATMENT

        # Only the server's recomputed curve restores both scalar forecasts.
        # The repeated calendar snapshot contains the same event exactly once.
        _snapshot(
            aioclient_mock, v3=v3, reading=latest, entries=retained,
            ar2=_curve(*RECOMPUTED_FORECAST), late=True,
        )
        for _ in range(2):
            await _refresh(hass, entry)
            _assert_reading_and_calculations(hass, "166", NEW_READING)
            _assert_forecasts(hass, NEW_READING, *RECOMPUTED_FORECAST)
            assert await _calendar_range(hass) == new_event
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await hass.async_block_till_done()
