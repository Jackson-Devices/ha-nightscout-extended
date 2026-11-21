"""Differential math cases for the pinned integration and synthetic wire data."""

import asyncio
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.nightscout_extended import api, sensor
from custom_components.nightscout_extended.sgv_history import SGVHistory
from tests_contract.math_review.oracle import (
    admits_v3_current,
    at_utc_ms,
    exact,
    expected_forecast,
    fresh_reservoir_contact,
    maintenance_hours,
    utc_ms,
)

# A nonzero millisecond phase catches whole-second conversion and truncation.
_POLL_MS = 1_767_873_600_123
_SITE = "https://math-fake.invalid"
_UPLOADER = "synthetic-uploader"
_ABSENT = object()


def _iso(millis):
    return at_utc_ms(millis).isoformat(timespec="milliseconds")


def _wire_number(expected):
    # Compare directly with binary64 representation of an exact expectation.
    # Test inputs that exercise fractional passthrough include non-binary decimals.
    return None if expected is None else float(expected)


def _entity(key, data, *, calculations_available=True):
    descriptions = (*sensor.SENSORS, sensor._RESERVOIR_SENSOR)
    description = next(item for item in descriptions if item.key == key)
    entry = SimpleNamespace(
        entry_id="math-contract", unique_id=None, title="Synthetic math contract",
        data={"url": _SITE},
    )
    coordinator = SimpleNamespace(
        config_entry=entry, status={}, data=data, units="mmol",
        last_update_success=True, calculations_available=calculations_available,
    )
    return sensor.NightscoutSensor(coordinator, description)


def _forecast_data(anchor_ms, emitted, raw_time_ms):
    props = {"ar2": {"forecast": {"predicted": [
        {"mills": point_ms, "mgdl": value} for point_ms, value in emitted
    ]}}}
    if anchor_ms is not None:
        props["bgnow"] = {"last": 129, "mills": anchor_ms}
    data = api.parse_properties(props)
    # The live coordinator calls this same adapter after selecting stored raw SGV.
    api._apply_v3_glucose(data, (at_utc_ms(raw_time_ms), 143, "Flat"))
    assert data.glucose_time == at_utc_ms(raw_time_ms)
    return data


def _assert_forecast(data, anchor_ms, horizon, emitted):
    want = expected_forecast(anchor_ms, horizon, emitted)
    actual = data.forecast_at(horizon)
    entity = _entity(f"forecast_{horizon}", data)
    assert entity.available
    assert entity.native_unit_of_measurement == "mg/dL"
    assert entity.extra_state_attributes["source_time"] == (
        at_utc_ms(anchor_ms).isoformat() if anchor_ms is not None else None
    )
    if want is None:
        assert actual is None
        assert entity.native_value is None
        assert entity.extra_state_attributes["forecast_time"] is None
    else:
        want_ms, want_quantity = want
        assert actual is not None
        assert utc_ms(actual.time) == want_ms
        assert actual.mgdl == _wire_number(want_quantity)
        assert entity.native_value == _wire_number(want_quantity)
        assert entity.extra_state_attributes["forecast_time"] == at_utc_ms(want_ms).isoformat()


@pytest.mark.parametrize("horizon", [15, 30], ids=["15m", "30m"])
@pytest.mark.parametrize("offset_ms", [
    pytest.param(-180_001, id="early-by-3m-and-1ms"),
    pytest.param(-180_000, id="early-by-3m"),
    pytest.param(-179_999, id="early-by-3m-less-1ms"),
    pytest.param(-2_000, id="synthetic-minus-2s"),
    pytest.param(2_000, id="producer-plus-2s"),
    pytest.param(179_999, id="late-by-3m-less-1ms"),
    pytest.param(180_000, id="late-by-3m"),
    pytest.param(180_001, id="late-by-3m-and-1ms"),
])
def test_forecast_horizon_alignment(horizon, offset_ms):
    anchor_ms = _POLL_MS - 600_000
    point_ms = anchor_ms + horizon * 60_000 + offset_ms
    emitted = [(point_ms, "156.125")]
    data = _forecast_data(anchor_ms, emitted, anchor_ms + 2_000)
    _assert_forecast(data, anchor_ms, horizon, emitted)


@pytest.mark.parametrize(("anchor_present", "present_horizons"), [
    pytest.param(True, (15, 30), id="both"),
    pytest.param(True, (15,), id="15m-only"),
    pytest.param(True, (30,), id="30m-only"),
    pytest.param(True, (), id="neither"),
    pytest.param(False, (15, 30), id="missing-v2-anchor"),
])
def test_forecast_missing_and_nearest(anchor_present, present_horizons):
    v2_ms = _POLL_MS - 600_000
    anchor_ms = v2_ms if anchor_present else None
    emitted = []
    for horizon in present_horizons:
        target_ms = v2_ms + horizon * 60_000
        # Reverse arrival and distinct quantities expose ordered/first-point
        # selection and substitution of the other horizon.
        emitted.extend([(target_ms + 100_000, "199.25"), (target_ms + 2_000, str(100 + horizon))])
    data = _forecast_data(anchor_ms, emitted, v2_ms + 2_000)
    for horizon in (15, 30):
        _assert_forecast(data, anchor_ms, horizon, emitted)


@pytest.mark.parametrize(("skew_ms", "corroboration"), [
    pytest.param(0, "missing", id="poll-instant-no-bgnow"),
    pytest.param(59_999, "exact", id="59999ms-exact"),
    pytest.param(60_000, "exact", id="60000ms-exact"),
    pytest.param(60_001, "exact", id="60001ms-exact"),
    pytest.param(59_999, "wrong-time", id="59999ms-wrong-time"),
    pytest.param(59_999, "wrong-value", id="59999ms-wrong-value"),
    pytest.param(59_999, "missing", id="59999ms-missing-bgnow"),
])
async def test_glucose_future_admission(tmp_path, skew_ms, corroboration):
    past_ms = _POLL_MS - 333
    sample_ms = _POLL_MS + skew_ms
    rows = [
        {"id": "past", "date": past_ms, "sgv": 123, "direction": "Flat"},
        {"id": "candidate", "date": sample_ms, "sgv": 144.125, "direction": "SingleUp"},
    ]
    bgnow = {
        "exact": (sample_ms, "144.125"),
        "wrong-time": (sample_ms - 1, "144.125"),
        "wrong-value": (sample_ms, "144.124"),
        "missing": None,
    }[corroboration]

    async def read_range(start, end):
        return [row for row in rows if start <= row["date"] < end]

    def executor_job(action, *args):
        return asyncio.get_running_loop().run_in_executor(None, action, *args)

    source = SimpleNamespace(
        uses_v3=True, site_id="math-synthetic-site", read_range=read_range,
        change_fence=AsyncMock(return_value=None),
    )
    hass = SimpleNamespace(
        config=SimpleNamespace(path=tmp_path.joinpath), data={},
        async_add_executor_job=executor_job,
    )
    history = SGVHistory(hass, "math-future-boundary", source)
    # Stage the whole window at a later ingest instant. The exact 60,000 ms
    # reading must reach the store so this tests selection, not range filtering.
    await history.async_refresh(at_utc_ms(_POLL_MS + 2))
    assert history.select_current(at_utc_ms(sample_ms), None, None) == (
        at_utc_ms(sample_ms), 144.125, "SingleUp",
    )
    bgnow_time, bgnow_value = (None, None) if bgnow is None else (
        at_utc_ms(bgnow[0]), _wire_number(exact(bgnow[1])),
    )
    admitted = admits_v3_current(sample_ms, "144.125", poll_ms=_POLL_MS, bgnow=bgnow)
    want = (at_utc_ms(sample_ms), 144.125, "SingleUp") if admitted else (
        at_utc_ms(past_ms), 123, "Flat",
    )
    assert history.select_current(at_utc_ms(_POLL_MS), bgnow_time, bgnow_value) == want


def _reservoir_row(identity, contact_ms, upload_ms, quantity=_ABSENT):
    pump = {"status": {"timestamp": _iso(contact_ms)}}
    if quantity is not _ABSENT:
        pump["reservoir"] = quantity
    return {
        "identifier": identity, "device": _UPLOADER, "pump": pump,
        "date": upload_ms, "created_at": _iso(upload_ms),
    }


def _client(monkeypatch, v3, rows):
    client = api.NightscoutClient(None, _SITE, None)
    monkeypatch.setattr(client, "_use_v3", v3)
    monkeypatch.setattr(client, "_get", AsyncMock(return_value=rows))

    async def pages(_path, _query, _limit):
        yield rows

    monkeypatch.setattr(client, "_v3_pages", pages)
    return client


def _assert_reservoir(reading, contact_ms, expected):
    entity = _entity("insulin_remaining", api.NightscoutData(reservoir=reading))
    assert entity.native_unit_of_measurement == "U"
    assert entity.available == (expected is not None)
    if expected is None:
        assert reading is None
        assert entity.native_value is None
    else:
        assert reading is not None
        assert reading.uploader == _UPLOADER
        assert utc_ms(reading.source_time) == contact_ms
        assert reading.units == _wire_number(expected)
        assert entity.native_value == _wire_number(expected)
        assert entity.extra_state_attributes["source_time"] == at_utc_ms(contact_ms).isoformat()


@pytest.mark.parametrize("v3", [False, True], ids=["v1", "v3"])
@pytest.mark.parametrize("elapsed_ms", [
    pytest.param(-1, id="future-1ms"),
    pytest.param(0, id="contact-now"),
    pytest.param(1, id="contact-1ms-ago"),
    pytest.param(3_599_999, id="under-hour-1ms"),
    pytest.param(3_600_000, id="exact-hour"),
    pytest.param(3_600_001, id="over-hour-1ms"),
])
async def test_reservoir_freshness(monkeypatch, v3, elapsed_ms):
    contact_ms = _POLL_MS - elapsed_ms
    # A fresh upload is constant across these cases, even when the pump
    # contact is stale. Only contact age should determine reading freshness.
    rows = [_reservoir_row("contact", contact_ms, _POLL_MS + 100, 8.125)]
    reading = await _client(monkeypatch, v3, rows).async_get_reservoir(
        at_utc_ms(_POLL_MS), _UPLOADER,
    )
    expected = exact("8.125") if fresh_reservoir_contact(contact_ms, observed_ms=_POLL_MS) else None
    _assert_reservoir(reading, contact_ms, expected)


def test_reservoir_contact_orders_before_upload_clock():
    fresh_ms = _POLL_MS - 1_000
    rows = [
        _reservoir_row("later-upload", fresh_ms - 1, _POLL_MS, 31),
        _reservoir_row("later-contact", fresh_ms, _POLL_MS - 1, 8.125),
    ]
    reading = api.select_reservoir(rows, at_utc_ms(_POLL_MS), _UPLOADER)
    _assert_reservoir(reading, fresh_ms, exact("8.125"))


@pytest.mark.parametrize(("quantity", "expected"), [
    pytest.param(0, "0", id="zero-units"),
    pytest.param(8.125, "8.125", id="fractional-units"),
    pytest.param(0.1, "0.1", id="decimal-units"),
    pytest.param(_ABSENT, None, id="missing-quantity"),
    pytest.param(None, None, id="null-quantity"),
    pytest.param(-0.125, None, id="negative-quantity"),
    pytest.param(True, None, id="boolean-quantity"),
    pytest.param("8.125", None, id="string-not-wire-number"),
])
def test_reservoir_quantity(quantity, expected):
    contact_ms = _POLL_MS - 500
    rows = [
        _reservoir_row("prior", contact_ms - 1, _POLL_MS - 1, 41),
        _reservoir_row("latest", contact_ms, _POLL_MS, quantity),
    ]
    reading = api.select_reservoir(rows, at_utc_ms(_POLL_MS), _UPLOADER)
    _assert_reservoir(reading, contact_ms, None if expected is None else exact(expected))


@pytest.mark.parametrize("v3", [False, True], ids=["v1", "v3"])
@pytest.mark.parametrize(("local_event", "local_now"), [
    pytest.param("2026-03-29T00:30:00.123", "2026-03-29T02:30:00.124", id="spring-forward"),
    pytest.param("2026-10-25T00:30:00.123", "2026-10-25T02:30:00.124", id="fall-back"),
])
async def test_maintenance_age_uses_utc_elapsed(monkeypatch, v3, local_event, local_now):
    london = ZoneInfo("Europe/London")
    event = datetime.fromisoformat(local_event).replace(tzinfo=london)
    now = datetime.fromisoformat(local_now).replace(tzinfo=london)
    event_ms, now_ms = utc_ms(event), utc_ms(now)
    families = {
        "insulin_age": "Insulin Change",
        "cannula_age": "Site Change",
        "sensor_age": "Sensor Change",
        "pump_battery_age": "Pump Battery Change",
    }
    rows = [
        {
            "identifier": key, "eventType": kind, "created_at": event.isoformat(),
            "date": event_ms, "srvModified": now_ms + 1000,
        }
        for key, kind in families.items()
    ]
    rows.append({
        "identifier": "future-insulin", "eventType": "Insulin Change",
        "date": now_ms + 1, "created_at": _iso(now_ms + 1),
    })
    client = _client(monkeypatch, v3, rows)
    markers = await client.async_get_age_markers(tuple(families.values()), now.astimezone(UTC))
    monkeypatch.setattr(sensor.dt_util, "utcnow", lambda: now.astimezone(UTC))
    data = api.NightscoutData(age_markers=markers)
    for key, kind in families.items():
        entity = _entity(key, data)
        expected = maintenance_hours(event_ms, observed_ms=now_ms)
        assert entity.available
        assert entity.native_unit_of_measurement == "h"
        assert entity.native_value == pytest.approx(_wire_number(expected), rel=0, abs=1e-12)
        assert entity.extra_state_attributes["event_type"] == kind
        assert utc_ms(datetime.fromisoformat(entity.extra_state_attributes["source_time"])) == event_ms


def test_maintenance_age_missing_or_failed(monkeypatch):
    monkeypatch.setattr(sensor.dt_util, "utcnow", lambda: at_utc_ms(_POLL_MS))
    for markers, expected_available in (({}, True), (None, False)):
        entity = _entity("insulin_age", api.NightscoutData(age_markers=markers))
        assert entity.available is expected_available
        assert entity.native_value is None


@pytest.mark.parametrize(("wire", "values"), [
    pytest.param(
        '{"iob":{"iob":0,"source":"synthetic-iob"},"cob":{"cob":0,"source":"synthetic-cob"},'
        '"basal":{"current":{"totalbasal":0}}}',
        ("0", "0", "0"), id="zero-in-each-unit",
    ),
    pytest.param(
        '{"iob":{"iob":0.125,"source":"synthetic-iob"},"cob":{"cob":12.1,"source":"synthetic-cob"},'
        '"basal":{"current":{"totalbasal":0.025}}}',
        ("0.125", "12.1", "0.025"), id="fractional-passthrough",
    ),
    pytest.param(
        '{"iob":{"iob":-0.125},"cob":{"cob":false},"basal":{"current":{"totalbasal":"NaN"}}}',
        ("-0.125", None, None), id="signed-iob-and-invalid-other-values",
    ),
    pytest.param(
        '{"iob":{"iob":"invalid"},"cob":{"cob":-0.5},"basal":{"current":{"totalbasal":-0.125}}}',
        (None, "-0.5", "-0.125"), id="invalid-iob-and-signed-other-values",
    ),
    pytest.param('{"cob":{"cob":2.125}}', (None, "2.125", None), id="one-of-three-properties"),
    pytest.param('{}', (None, None, None), id="no-optional-properties"),
])
def test_device_status_quantity_passthrough(wire, values):
    # These values arrive from Nightscout's calculated v2 properties.
    # HA preserves the quantity and the unit without recomputing physiology
    # or applying the site's glucose display unit to IOB, COB, or basal.
    properties = json.loads(wire)
    data = api.parse_properties(properties)
    for key, unit, literal in zip(("iob", "cob", "basal"), ("U", "g", "U/h"), values, strict=True):
        entity = _entity(key, data)
        assert entity.available
        assert entity.native_unit_of_measurement == unit
        expected = exact(literal) if literal is not None else None
        assert entity.native_value == _wire_number(expected)
        if key in {"iob", "cob"}:
            assert entity.extra_state_attributes["source"] == properties.get(key, {}).get("source")
        failed_entity = _entity(key, data, calculations_available=False)
        assert not failed_entity.available
