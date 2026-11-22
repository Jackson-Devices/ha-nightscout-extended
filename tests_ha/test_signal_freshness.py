"""Observe Nightscout sensor freshness through Home Assistant's scheduled callbacks."""

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.const import CONF_API_KEY, CONF_URL, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nightscout_extended.const import CONF_SCAN_INTERVAL, DOMAIN

from test_sgv_ingestion import (
    PROPERTIES,
    SUBJECT,
    _Nightscout,
    _configure,
    _current,
    _entity,
    _history,
    _ms,
    _unload,
)

READING = datetime(2026, 1, 8, 12, tzinfo=UTC)
ONE_MS = timedelta(milliseconds=1)
POINTS = (
    (READING + timedelta(minutes=15, seconds=2), 207),
    (READING + timedelta(minutes=30, seconds=2), 231),
)
GLUCOSE_SIGNALS = ("glucose", "glucose_delta", "glucose_trend")


async def _at(hass, freezer, when):
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _observe_state_changes(hass, entities):
    changes = []

    @callback
    def record(event):
        old = event.data["old_state"]
        new = event.data["new_state"]
        if old is not None and new is not None and old.state != new.state:
            changes.append((event.data["entity_id"], old.state, new.state))

    return changes, async_track_state_change_event(hass, entities, record)


def _set_reading(site, identity, measured):
    site.put(identity, measured, 124, direction="FortyFiveUp")
    site.bgnow = {"last": 124, "mills": _ms(measured)}
    site.calculations.update({
        "delta": {"mgdl": 7},
        "direction": {"value": "FortyFiveUp"},
        "iob": {"iob": 1.35, "source": "fixture-pump"},
        "cob": {"cob": 28.9, "source": "fixture-meal"},
    })


def _set_curve(site, points):
    site.calculations["ar2"] = {
        "forecast": {"predicted": [{"mills": _ms(at), "mgdl": value} for at, value in points]},
    }


def _forecast_state(hass, entry, key, source, point, expected):
    state = hass.states.get(_entity(hass, entry, key))
    assert state.state == expected
    assert state.attributes["source_time"] == source.isoformat()
    assert state.attributes["forecast_time"] == point[0].isoformat()


def _assert_curve(hass, entry, points):
    attrs = hass.states.get(_entity(hass, entry, "forecast_30")).attributes
    assert attrs["model"] == "AR2"
    assert attrs["forecast"] == [{"time": at.isoformat(), "mgdl": value} for at, value in points]


def _assert_current(hass, entry, measured, *, valid):
    glucose, delta, trend = ("124", "7", "forty_five_up") if valid else (STATE_UNKNOWN,) * 3
    reading = hass.states.get(_entity(hass, entry, "glucose"))
    _current(hass, entry, glucose, measured.isoformat())
    assert hass.states.get(_entity(hass, entry, "glucose_delta")).state == delta
    assert hass.states.get(_entity(hass, entry, "glucose_trend")).state == trend
    assert reading.attributes.get("delta") == (7 if valid else None)
    assert reading.attributes.get("direction") == ("FortyFiveUp" if valid else None)
    for key, value, source in (("iob", "1.35", "fixture-pump"), ("cob", "28.9", "fixture-meal")):
        sibling = hass.states.get(_entity(hass, entry, key))
        assert sibling.state == value
        assert sibling.attributes["source"] == source


@pytest.mark.parametrize("v3", [False, True], ids=["legacy", "v3"])
async def test_ar2_horizons_expire_at_selected_points_without_network(hass, aioclient_mock, freezer, v3):
    """Each scalar changes through HA state events at its actual AR2 point time."""
    site = _Nightscout(aioclient_mock, v3=v3)
    site.status_settings = {
        "alarmTimeagoWarn": True,
        "alarmTimeagoWarnMins": 60,
        "alarmTimeagoUrgent": True,
        "alarmTimeagoUrgentMins": 75,
    }
    # Place one real scheduled poll between the two target boundaries. A
    # replay of the whole response at that poll cannot restore the first.
    start = READING + timedelta(minutes=14, seconds=55)
    freezer.move_to(start)
    _set_reading(site, "initial", READING)
    _set_curve(site, POINTS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=site.url,
        title=site.name,
        data={CONF_URL: site.url, CONF_API_KEY: SUBJECT},
        options={CONF_SCAN_INTERVAL: 900},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    first_id = _entity(hass, entry, "forecast_15")
    second_id = _entity(hass, entry, "forecast_30")
    changes, unsubscribe = _observe_state_changes(hass, [first_id, second_id])
    try:
        for target, expected in (
            (POINTS[0][0] - ONE_MS, "207"),
            (POINTS[0][0], STATE_UNKNOWN),
            (POINTS[0][0] + ONE_MS, STATE_UNKNOWN),
        ):
            requests = len(site.requests)
            await _at(hass, freezer, target)
            _forecast_state(hass, entry, "forecast_15", READING, POINTS[0], expected)
            _forecast_state(hass, entry, "forecast_30", READING, POINTS[1], "231")
            _assert_curve(hass, entry, POINTS)
            _assert_current(hass, entry, READING, valid=True)
            assert len(site.requests) == requests
        assert changes == [(first_id, "207", STATE_UNKNOWN)]

        replay = start + timedelta(seconds=901)
        request_mark = len(site.requests)
        await _at(hass, freezer, replay)
        assert entry.runtime_data.last_update_success
        assert PROPERTIES in {path for path, _ in site.requests[request_mark:]}
        _forecast_state(hass, entry, "forecast_15", READING, POINTS[0], STATE_UNKNOWN)
        _forecast_state(hass, entry, "forecast_30", READING, POINTS[1], "231")

        for target, expected in (
            (POINTS[1][0] - ONE_MS, "231"),
            (POINTS[1][0], STATE_UNKNOWN),
            (POINTS[1][0] + ONE_MS, STATE_UNKNOWN),
        ):
            requests = len(site.requests)
            await _at(hass, freezer, target)
            _forecast_state(hass, entry, "forecast_15", READING, POINTS[0], STATE_UNKNOWN)
            _forecast_state(hass, entry, "forecast_30", READING, POINTS[1], expected)
            _assert_curve(hass, entry, POINTS)
            _assert_current(hass, entry, READING, valid=True)
            assert len(site.requests) == requests
        assert changes == [(first_id, "207", STATE_UNKNOWN), (second_id, "231", STATE_UNKNOWN)]

        renewed = replay + timedelta(seconds=900)
        new_points = tuple((when + (renewed - READING), value) for when, value in POINTS)
        freezer.move_to(renewed)
        _set_reading(site, "renewed", renewed)
        _set_curve(site, new_points)
        request_mark = len(site.requests)
        await _at(hass, freezer, renewed + timedelta(seconds=1))
        assert PROPERTIES in {path for path, _ in site.requests[request_mark:]}
        _forecast_state(hass, entry, "forecast_15", renewed, new_points[0], "207")
        _forecast_state(hass, entry, "forecast_30", renewed, new_points[1], "231")
        _assert_curve(hass, entry, new_points)
        assert sorted(changes[-2:]) == sorted([
            (first_id, STATE_UNKNOWN, "207"), (second_id, STATE_UNKNOWN, "231"),
        ])
    finally:
        unsubscribe()
        await _unload(hass, entry)


@pytest.mark.parametrize("v3", [False, True], ids=["legacy", "v3"])
@pytest.mark.parametrize(
    ("settings", "minutes", "expires"),
    [
        pytest.param({}, 15, True, id="stock-default-warning"),
        pytest.param(
            {"alarmTimeagoWarn": False, "alarmTimeagoWarnMins": 1, "alarmTimeagoUrgent": True},
            30, True, id="stock-default-urgent",
        ),
        pytest.param(
            {"alarmTimeagoWarn": True, "alarmTimeagoWarnMins": 4,
             "alarmTimeagoUrgent": True, "alarmTimeagoUrgentMins": 7},
            4, True, id="earlier-site-warning",
        ),
        pytest.param(
            {"alarmTimeagoWarn": True, "alarmTimeagoWarnMins": 7,
             "alarmTimeagoUrgent": True, "alarmTimeagoUrgentMins": 4},
            4, True, id="earlier-site-urgent",
        ),
        pytest.param(
            {"alarmTimeagoWarn": True, "alarmTimeagoWarnMins": 7,
             "alarmTimeagoUrgent": False, "alarmTimeagoUrgentMins": 1},
            7, True, id="urgent-disabled",
        ),
        pytest.param(
            {"alarmTimeagoWarn": False, "alarmTimeagoWarnMins": 1,
             "alarmTimeagoUrgent": False, "alarmTimeagoUrgentMins": 2},
            45, False, id="both-disabled",
        ),
    ],
)
async def test_source_timeago_expires_strictly_after_enabled_threshold(
    hass, aioclient_mock, freezer, v3, settings, minutes, expires,
):
    deadline = READING + timedelta(minutes=minutes)
    start = deadline - timedelta(seconds=5)
    freezer.move_to(start)
    site = _Nightscout(aioclient_mock, v3=v3)
    site.status_settings = settings
    _set_reading(site, "original", READING)
    entry = await _configure(hass, site)

    entities = {
        _entity(hass, entry, key): state
        for key, state in zip(GLUCOSE_SIGNALS, ("124", "7", "forty_five_up"), strict=True)
    }
    changes, unsubscribe = _observe_state_changes(hass, list(entities))
    try:
        beginning = READING - timedelta(minutes=1)
        end = READING + ONE_MS
        requests = len(site.requests)
        committed = await _history(hass, entry, beginning.isoformat(), end.isoformat())
        assert (committed["status"], committed["missing_ranges"]) == ("complete", [])
        assert [(row["date"], row["sgv"]) for row in committed["readings"]] == [(_ms(READING), 124)]

        for when, valid in ((deadline - ONE_MS, True), (deadline, True), (deadline + ONE_MS, not expires)):
            await _at(hass, freezer, when)
            _assert_current(hass, entry, READING, valid=valid)
            assert await _history(hass, entry, beginning.isoformat(), end.isoformat()) == committed
            assert len(site.requests) == requests
        assert sorted(changes) == (
            sorted((entity_id, state, STATE_UNKNOWN) for entity_id, state in entities.items())
            if expires else []
        )

        # A storage update with the same glucose amount is fresh only because
        # Nightscout supplies a new measurement timestamp.
        fresh = start + timedelta(seconds=60)
        freezer.move_to(fresh)
        _set_reading(site, "fresh", fresh)
        await _at(hass, freezer, fresh + timedelta(seconds=1))
        assert PROPERTIES in {path for path, _ in site.requests[requests:]}
        assert entry.runtime_data.last_update_success
        _assert_current(hass, entry, fresh, valid=True)
        assert sorted(changes[len(entities):]) == (
            sorted((entity_id, STATE_UNKNOWN, state) for entity_id, state in entities.items())
            if expires else []
        )
        requests = len(site.requests)
        history = await _history(hass, entry, beginning.isoformat(), (fresh + ONE_MS).isoformat())
        assert (history["status"], history["missing_ranges"]) == ("complete", [])
        assert [(row["date"], row["sgv"]) for row in history["readings"]] == [
            (_ms(READING), 124), (_ms(fresh), 124),
        ]
        assert len(site.requests) == requests
    finally:
        unsubscribe()
        await _unload(hass, entry)
