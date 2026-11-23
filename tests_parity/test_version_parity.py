"""Live stock v1/v2/v3 wire parity and the fork's implemented HA legacy path."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import re

import pytest
from homeassistant.const import CONF_API_KEY, CONF_URL, STATE_UNKNOWN, UnitOfBloodGlucoseConcentration
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_conversion import BloodGlucoseConcentrationConverter
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nightscout_extended.api import NightscoutClient
from custom_components.nightscout_extended.const import CONF_USE_API_SECRET, DOMAIN

from .conftest import BASE_URL
from .stock_contract import StockContract
from .version_stock import (
    DEBUG_PERMISSION, EPOCH, PROPERTIES, READABLE, STOCK_VERSION, StockReads, alternate_seed_rows,
    await_stock, instant, iso, millis, named_read_subject, read_subject, request, require, seeded_collections,
    synthetic_data,
)

LEGACY_COLLECTIONS = {
    "entries": "entries/sgv.json", "treatments": "treatments.json",
    "devicestatus": "devicestatus/", "profile": "profile/",
}
THRESHOLDS = {
    "urgent_low": "bgLow", "low": "bgTargetBottom",
    "high": "bgTargetTop", "urgent_high": "bgHigh",
}
SETTINGS = (
    "units", "thresholds", "alarmTimeagoWarn", "alarmTimeagoWarnMins",
    "alarmTimeagoUrgent", "alarmTimeagoUrgentMins", "theme",
)
SENSOR_KEYS = (
    "glucose", "glucose_delta", "glucose_trend", "last_reading",
    "iob", "cob", "basal", "last_carbs", "last_insulin", "forecast_15", "forecast_30",
)
MGDL = UnitOfBloodGlucoseConcentration.MILLIGRAMS_PER_DECILITER
MMOL = UnitOfBloodGlucoseConcentration.MILLIMOLE_PER_LITER


def v3_result(body):
    assert isinstance(body, dict) and body.get("status") == 200 and "result" in body, "v3 result envelope"
    return body["result"]


def _seed_rows(body, ids: set[str], api: int) -> dict[str, dict]:
    rows = v3_result(body) if api == 3 else body
    assert isinstance(rows, list), f"v{api} did not return a collection array"
    seed = {}
    for row in rows:
        assert isinstance(row, dict), f"v{api} returned a non-document member"
        identity = row.get("identifier") if api == 3 else row.get("_id")
        if identity in ids:
            assert identity not in seed, f"v{api} repeated a seeded document"
            seed[identity] = row
    assert seed.keys() == ids, f"v{api} did not return every seeded identity"
    return seed


def _collection_query(name: str, api: int, anchor: datetime) -> dict[str, str]:
    if name == "profile":
        return {"limit": "40"} if api == 3 else {}
    lower = anchor - timedelta(days=6)
    upper = anchor + timedelta(minutes=30)
    if api == 3:
        query = {"limit": "40"}
        if name == "entries":
            query.update({"type": "sgv", "date$gte": str(millis(lower)), "date$lt": str(millis(upper)),
                          "sort$desc": "date"})
        else:
            query.update({"created_at$gte": iso(lower), "created_at$lt": iso(upper), "sort$desc": "created_at"})
        return query
    query = {"count": "40"}
    if name == "entries":
        query.update({"find[date][$gte]": str(millis(lower)), "find[date][$lt]": str(millis(upper))})
    else:
        query.update({"find[created_at][$gte]": iso(lower), "find[created_at][$lt]": iso(upper)})
    return query


def _permissions(wire: StockReads, reader) -> None:
    """Prove the normal product credential and legacy bearer share v3's effective scope."""
    for domain in ("status", *READABLE):
        for action in ("read", "create", "update", "delete", "admin"):
            endpoint = f"{DEBUG_PERMISSION}/api:{domain}:{action}"
            outcome = 200 if action == "read" else 401
            for legacy_query in (True, False):
                response = wire.exchange(endpoint, subject=reader, expected_status=outcome,
                                         contract_path=f"{DEBUG_PERMISSION}/<permission>", legacy_query=legacy_query)
                if outcome == 200:
                    assert response == {"check": True}, f"missing read permission for {domain}"


def _await_reading(expected: dict, reader, *, usable: bool) -> None:
    def observed():
        status, _, props = request(PROPERTIES, headers=reader.headers)
        if status != 200 or not isinstance(props, dict):
            return False
        bgnow = props.get("bgnow") or {}
        if usable:
            return (bgnow.get("last"), bgnow.get("mills")) == (expected["sgv"], expected["date"])
        errors = bgnow.get("errors") or []
        return bgnow.get("last") is None and any(
            (row.get("mgdl"), row.get("mills")) == (expected["sgv"], expected["date"]) for row in errors
        )

    await_stock(observed, "seed-specific bgnow")


@pytest.fixture(scope="module")
def stock_versions(stock, native_lib):
    contract = StockContract.from_stock_lib(native_lib)
    status, media, body = request("/swagger.json")
    require(status == 200 and media.split(";")[0] == "application/json", "published v1 Swagger was unavailable")
    contract.verify_served_v1(body)

    anchor = datetime.now(UTC).replace(microsecond=321000) - timedelta(seconds=45)
    documents, alternate = synthetic_data(anchor)
    with seeded_collections(stock, documents, alternate), named_read_subject(stock) as name:
        yield contract, name, anchor, documents, alternate


@pytest.fixture(scope="module")
def stock_reads(stock_versions):
    return StockReads(stock_versions[0])


@pytest.fixture(scope="module")
def stock_wire(stock, stock_versions, stock_reads):
    contract, name, anchor, docs, _ = stock_versions
    reader = read_subject(stock, name, contract)
    _await_reading(docs["entries"][-1], reader, usable=True)
    wire = stock_reads
    wire.attest_legacy_subject(reader, name)
    _permissions(wire, reader)

    version = v3_result(wire.exchange("/api/v3/version"))
    status3 = v3_result(wire.exchange("/api/v3/status", subject=reader))
    for result in (version, status3):
        assert result["version"] == STOCK_VERSION
        assert str(result["apiVersion"]).split(".")[0] == "3"
    assert status3["apiPermissions"] == dict.fromkeys(READABLE, "r")
    wire.exchange("/api/v3/entries", params={"limit": "1"}, expected_status=401)
    # /status advertises settings:r; the settings collection requires settings:admin.
    wire.exchange("/api/v3/settings", subject=reader, expected_status=403)

    legacy_status, collections, missing = {}, {}, {}
    for api in (1, 2, 3):
        if api != 3:
            legacy = wire.exchange(f"/api/v{api}/status.json", subject=reader)
            require(legacy.get("authorized") is None, "legacy status unexpectedly returned auth material")
            legacy_status[api] = legacy
        for name, legacy in LEGACY_COLLECTIONS.items():
            path = f"/api/v{api}/{name if api == 3 else legacy}"
            payload = wire.exchange(path, subject=reader, params=_collection_query(name, api, anchor))
            collections[api, name] = _seed_rows(payload, {row["_id"] for row in docs[name]}, api)
        treatment_path = f"/api/v{api}/" + ("treatments" if api == 3 else "treatments.json")
        event_type = "eventType$eq" if api == 3 else "find[eventType]"
        body = wire.exchange(treatment_path, subject=reader, params={event_type: "Synthetic Not Seeded"})
        missing[api] = v3_result(body) if api == 3 else body

    props = wire.exchange(PROPERTIES, subject=reader)
    return wire, legacy_status, collections, missing, props


def _assert_original_time(row: dict, expected: dict, field: str) -> None:
    when = instant(row[field])
    assert when == instant(expected[field])
    assert row["date"] == expected["date"] == millis(when)


def test_same_seed_and_read_scope_agree_on_stock_measurements(stock, stock_versions, stock_wire):
    _, _, _, docs, _ = stock_versions
    _, legacy_status, collections, empty_treatments, props = stock_wire

    for status in legacy_status.values():
        assert status["version"] == STOCK_VERSION and status["status"] == "ok" and status["apiEnabled"] is True
    settings = {key: legacy_status[1]["settings"][key] for key in SETTINGS}
    assert {key: legacy_status[2]["settings"][key] for key in SETTINGS} == settings
    # Served in mg/dL (lib/settings.js); stock.sh sets BG_LOW=0 for exactly one site, and a 0 stays 0.
    thresholds = settings["thresholds"]
    assert all(type(value) in (int, float) for value in thresholds.values())
    assert all(value >= 30 for key, value in thresholds.items() if key != "bgLow")
    if stock.profile == "mgdl-custom":
        assert thresholds["bgLow"] == 0
    else:
        assert thresholds["bgLow"] >= 30
    assert type(settings["alarmTimeagoWarn"]) is bool
    assert type(settings["alarmTimeagoUrgent"]) is bool
    # stock.sh sets WARN_MINS in the environment for exactly one site.
    assert settings["alarmTimeagoWarnMins"] == ("20" if stock.profile == "mgdl-custom" else 15)
    assert settings["alarmTimeagoUrgentMins"] == 30
    assert empty_treatments == {1: [], 2: [], 3: []}

    for api in (1, 2, 3):
        for seed in docs["entries"]:
            raw = collections[api, "entries"][seed["_id"]]
            assert (raw["date"], raw["sgv"], raw["direction"], raw["type"], raw["device"]) == (
                seed["date"], seed["sgv"], seed["direction"], "sgv", seed["device"],
            )
            if api != 3 or "dateString" in raw:
                _assert_original_time(raw, seed, "dateString")
        for seed in docs["treatments"]:
            raw = collections[api, "treatments"][seed["_id"]]
            _assert_original_time(raw, seed, "created_at")
            assert {k: raw[k] for k in ("eventType", "notes", "enteredBy")} == {
                k: seed[k] for k in ("eventType", "notes", "enteredBy")
            }
            for field in ("duration", "rate", "absolute"):
                assert (field in raw) == (field in seed)
                assert raw.get(field) == seed.get(field)
            for field in ("carbs", "insulin"):
                if api == 3:
                    assert (field in raw) == (field in seed)
                    assert raw.get(field) == seed.get(field)
                    if field in seed:
                        assert type(raw[field]) is type(seed[field]) or (
                            type(seed[field]) in (int, float) and type(raw[field]) in (int, float)
                        )
                else:
                    # Stock v1 serveTreatments() uses Number, including Number(undefined) -> JSON null.
                    assert field in raw
                    value = seed.get(field)
                    assert raw[field] == (float(value) if value is not None else None)
                    assert raw[field] is None or type(raw[field]) in (int, float)
        for seed in docs["devicestatus"]:
            raw = collections[api, "devicestatus"][seed["_id"]]
            _assert_original_time(raw, seed, "created_at")
            assert raw["device"] == seed["device"]
            assert raw["pump"]["reservoir"] == seed["pump"]["reservoir"]
            assert instant(raw["pump"]["status"]["timestamp"]) == instant(seed["pump"]["status"]["timestamp"])
            assert instant(raw["pump"]["status"]["timestamp"]) != instant(raw["created_at"])
            assert raw.get("loop") == seed.get("loop")
        [seed] = docs["profile"]
        raw = collections[api, "profile"][seed["_id"]]
        assert instant(raw["startDate"]) == instant(seed["startDate"])
        assert raw["defaultProfile"] == seed["defaultProfile"]
        for field in ("basal", "sens", "carbratio"):
            assert raw["store"]["Synthetic"][field] == seed["store"]["Synthetic"][field]

    # Reconcile live plugin outputs with original stock collection fields before using them as HA expectations.
    current, previous = (collections[3, "entries"][row["_id"]] for row in docs["entries"][-1:-3:-1])
    assert (props["bgnow"]["last"], props["bgnow"]["mills"]) == (current["sgv"], current["date"])
    assert props["delta"]["times"] == {"recent": current["date"], "previous": previous["date"]}
    assert props["delta"]["mgdl"] == current["sgv"] - previous["sgv"]
    assert props["direction"]["value"] == current["direction"]
    latest_device = collections[3, "devicestatus"][docs["devicestatus"][-1]["_id"]]
    for field in ("iob", "cob"):
        original = latest_device["loop"][field]
        assert (props[field][field], props[field]["source"], props[field]["mills"]) == (
            original[field], "Loop", millis(instant(original["timestamp"])),
        )
    [seed] = docs["profile"]
    basal = collections[3, "profile"][seed["_id"]]["store"]["Synthetic"]["basal"][0]["value"]
    assert props["basal"]["current"]["totalbasal"] == basal
    points = props["ar2"]["forecast"]["predicted"]
    assert [point["mills"] for point in points] == [
        current["date"] + 300_000 * step + 2_000 for step in range(1, 7)
    ]
    assert all(type(p["mgdl"]) in (int, float) and 36 <= p["mgdl"] <= 400 for p in points)


@asynccontextmanager
async def _integration(hass, reader, monkeypatch):
    requests = []
    original_get = NightscoutClient._get

    async def _observed_get(client, path, query=None, **options):
        requests.append(path)
        return await original_get(client, path, query, **options)

    monkeypatch.setattr(NightscoutClient, "_get", _observed_get)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=BASE_URL, title="Version Parity",
        data={CONF_URL: BASE_URL, CONF_API_KEY: reader.access,
              CONF_USE_API_SECRET: callable(getattr(NightscoutClient, "_select_v3", None))},
    )
    entry.add_to_hass(hass)
    try:
        require(await hass.config_entries.async_setup(entry.entry_id), "legacy integration could not read stock")
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        entities = {
            row.translation_key: row for row in er.async_entries_for_config_entry(registry, entry.entry_id)
            if row.platform == DOMAIN
        }
        basal = entities.get("basal")
        assert basal is not None, "basal sensor was not registered"
        if basal.disabled_by is not None:
            registry.async_update_entity(basal.entity_id, disabled_by=None)
            require(await hass.config_entries.async_reload(entry.entry_id), "basal opt-in reload failed")
            await hass.async_block_till_done()
        yield entry, entities, requests
    finally:
        if entry.state.name == "LOADED":
            require(await hass.config_entries.async_unload(entry.entry_id), "stock HA entry could not unload")
            await hass.async_block_till_done()


def _state(hass, entities, key):
    assert key in entities, f"{key} is not registered"
    state = hass.states.get(entities[key].entity_id)
    assert state is not None, f"{key} has no HA state"
    return state


def _assert_basal_matches_stock(data, state, stock_basal):
    assert data.basal == stock_basal
    if stock_basal is None:
        assert state.state == STATE_UNKNOWN
    else:
        assert type(stock_basal) in (int, float)
        assert float(state.state) == pytest.approx(stock_basal)
        assert state.attributes["unit_of_measurement"] == "U/h"


def _storage_fence(wire, reader):
    fields = v3_result(wire.exchange("/api/v3/lastModified", subject=reader))["collections"]
    assert all(type(fields.get(key)) is int and fields[key] > 0 for key in LEGACY_COLLECTIONS)
    return {key: fields[key] for key in LEGACY_COLLECTIONS}


async def test_implemented_legacy_integration_matches_independent_stock_reads(
    hass, stock, stock_versions, stock_wire, monkeypatch, record_property,
):
    contract, name, anchor, _, _ = stock_versions
    wire, statuses, collections, _, props = stock_wire
    reader = await hass.async_add_executor_job(read_subject, stock, name, contract)
    before = await hass.async_add_executor_job(_storage_fence, wire, reader)
    async with _integration(hass, reader, monkeypatch) as (entry, entities, observed):
        actual = {key: _state(hass, entities, key) for key in SENSOR_KEYS}
        data = entry.runtime_data.data
        settings = statuses[1]["settings"]
        unit = MMOL if settings["units"].lower().startswith("mmol") else MGDL
        convert = BloodGlucoseConcentrationConverter.converter_factory(MGDL, unit)
        reading = props["bgnow"]
        when = EPOCH + timedelta(milliseconds=reading["mills"])
        trend = props["direction"]["value"]
        assert float(actual["glucose"].state) == pytest.approx(convert(reading["last"]))
        assert actual["glucose"].attributes["unit_of_measurement"] == unit
        assert actual["glucose"].attributes["direction"] == trend
        assert actual["glucose"].attributes["delta"] == props["delta"]["mgdl"]
        assert float(actual["glucose_delta"].state) == pytest.approx(convert(props["delta"]["mgdl"]))
        assert actual["glucose_trend"].state == re.sub(r"(?<=[a-z])(?=[A-Z])", "_", trend).lower()
        assert data.glucose_time == when
        assert instant(actual["last_reading"].state) == when.replace(microsecond=0)
        assert actual["glucose"].attributes["thresholds_mgdl"] == {
            role: float(settings["thresholds"][key]) for role, key in THRESHOLDS.items()
        }
        for key, signal_unit in (("iob", "U"), ("cob", "g")):
            assert float(actual[key].state) == props[key][key]
            assert actual[key].attributes["source"] == props[key]["source"]
            assert actual[key].attributes["unit_of_measurement"] == signal_unit
        _assert_basal_matches_stock(data, actual["basal"], props["basal"]["current"]["totalbasal"])

        points = props["ar2"]["forecast"]["predicted"]
        for horizon in (15, 30):
            target = next(point for point in points if point["mills"] == reading["mills"] + horizon * 60_000 + 2_000)
            assert float(actual[f"forecast_{horizon}"].state) == pytest.approx(convert(target["mgdl"]))
        assert actual["forecast_30"].attributes["model"] == "AR2"
        assert actual["forecast_30"].attributes["forecast"] == [
            {"time": (EPOCH + timedelta(milliseconds=p["mills"])).isoformat(), "mgdl": round(p["mgdl"])}
            for p in points
        ]

        legacy = collections[1, "treatments"]
        observed_treatments = {t.uid: t for t in data.treatments}
        for identity, doc in legacy.items():
            at = instant(doc["created_at"])
            if at < anchor - timedelta(hours=47):
                continue
            row = observed_treatments[identity]
            assert (row.time, row.event_type, row.notes, row.entered_by, row.duration_min) == (
                at, doc["eventType"], doc["notes"], doc["enteredBy"], doc.get("duration"),
            )
            assert (row.carbs, row.insulin) == (doc["carbs"] or None, doc["insulin"] or None)
        for field in ("carbs", "insulin"):
            doc = max((doc for doc in legacy.values() if doc[field] and instant(doc["created_at"]) <= anchor),
                      key=lambda row: instant(row["created_at"]))
            state = actual[f"last_{field}"]
            assert float(state.state) == doc[field]
            assert instant(state.attributes["time"]) == instant(doc["created_at"])
            assert state.attributes["event_type"] == doc["eventType"]
            assert state.attributes["notes"] == doc["notes"]

        calendar = entities["treatments"].entity_id
        start, end = anchor - timedelta(minutes=95), anchor + timedelta(minutes=40)
        calendar_result = await hass.services.async_call(
            "calendar", "get_events",
            {"entity_id": calendar, "start_date_time": start, "end_date_time": end},
            blocking=True, return_response=True,
        )
        expected_events = {instant(row["created_at"]): row for row in legacy.values()
                           if start <= instant(row["created_at"]) < end}
        matching_events = [
            (instant(row["start"]), row) for row in calendar_result[calendar]["events"]
            if instant(row["start"]) in expected_events
        ]
        actual_events = dict(matching_events)
        assert len(actual_events) == len(matching_events), "calendar repeated a seeded event"
        assert actual_events.keys() == expected_events.keys()
        for event_at, doc in expected_events.items():
            state = actual_events[event_at]
            assert instant(state["end"]) == event_at + timedelta(minutes=doc.get("duration") or 5)
            summary = doc["notes"]
            if doc["carbs"]:
                summary = f"{doc['eventType']}: {doc['carbs']:g} g carbs"
            if doc["insulin"]:
                summary = f"{doc['eventType']}: {doc['insulin']:g} U insulin"
            assert state["summary"] == summary
            assert state["description"] == f"{doc['notes']}\nEntered by {doc['enteredBy']}"

        assert {"/api/v1/status.json", "/api/v2/properties/bgnow", PROPERTIES,
                "/api/v1/treatments.json"} <= set(observed)
        assert not any(path.startswith("/api/v3/") for path in observed), "expected a legacy read"
        require(entry.runtime_data.client.uses_api_secret is entry.data[CONF_USE_API_SECRET],
                "legacy HA authentication changed from the selected mode")
        record_property("legacy_api", "observed v1 status/treatments and v2 properties")
        record_property("source_only", "v2 plugin values; forecast source time as stock bgnow time")
        record_property("status_only", "timeago/alarm configuration; no HA alarm entity")
    after = await hass.async_add_executor_job(_storage_fence, wire, reader)
    assert after == before, "stock document storage changed during the read-only HA comparison"


async def test_candidate_legacy_and_v3_match_same_stock_seed(
    hass, stock, stock_versions, stock_wire, record_property, tmp_path,
):
    # Keep the candidate readers in this module's existing stock fixture lifetime.
    from .test_candidate_modes import compare_candidate_modes

    await compare_candidate_modes(hass, stock, stock_versions, stock_wire, record_property, tmp_path)


@pytest.fixture(params=("stale", "missing"))
def alternate_stock_reading(request, stock, stock_versions):
    contract, name, _, docs, alternate = stock_versions
    mode = request.param
    seed = alternate[0] if mode == "stale" else alternate[1]
    with alternate_seed_rows(stock, "entries", docs["entries"], [seed]):
        reader = read_subject(stock, name, contract)
        _await_reading(seed, reader, usable=mode == "stale")
        yield mode, reader, seed


async def test_stock_original_time_distinguishes_stale_from_missing(
    hass, alternate_stock_reading, stock_reads, monkeypatch,
):
    mode, reader, seed = alternate_stock_reading
    wire = stock_reads
    props = await hass.async_add_executor_job(lambda: wire.exchange(PROPERTIES, subject=reader))
    for api in (1, 2, 3):
        path = f"/api/v{api}/" + ("entries" if api == 3 else "entries/sgv.json")
        params = {"limit": "5"} if api == 3 else {"count": "5"}
        body = await hass.async_add_executor_job(lambda: wire.exchange(path, subject=reader, params=params))
        rows = _seed_rows(body, {seed["_id"]}, api)
        assert (rows[seed["_id"]]["date"], rows[seed["_id"]]["sgv"]) == (seed["date"], seed["sgv"])

    async with _integration(hass, reader, monkeypatch) as (entry, entities, _):
        current = props.get("bgnow") or {}
        data = entry.runtime_data.data
        glucose = _state(hass, entities, "glucose")
        last_reading = _state(hass, entities, "last_reading")
        assert _state(hass, entities, "glucose_delta").state == STATE_UNKNOWN
        assert (props.get("delta") or {}).get("mgdl") is None
        assert not (((props.get("ar2") or {}).get("forecast") or {}).get("predicted") or [])
        for key in ("forecast_15", "forecast_30"):
            assert _state(hass, entities, key).state == STATE_UNKNOWN
        if mode == "stale":
            age = datetime.now(UTC) - instant(seed["dateString"])
            assert age > timedelta(minutes=15)
            assert (current["last"], current["mills"]) == (seed["sgv"], seed["date"])
            assert data.glucose_time == instant(seed["dateString"])
            assert instant(last_reading.state) == instant(seed["dateString"]).replace(microsecond=0)
            assert (props.get("direction") or {}).get("value") is None
            assert _state(hass, entities, "glucose_trend").state == STATE_UNKNOWN
            unit = glucose.attributes["unit_of_measurement"]
            convert = BloodGlucoseConcentrationConverter.converter_factory(MGDL, unit)
            assert float(glucose.state) == pytest.approx(convert(current["last"]))
        else:
            assert "last" not in current and "mills" not in current
            assert any((e["mgdl"], e["mills"]) == (10, seed["date"]) for e in current["errors"])
            assert data.glucose is None and data.glucose_time is None
            assert glucose.state == last_reading.state == STATE_UNKNOWN


def _read_stock_profiles(wire, reader):
    return {
        1: wire.exchange("/api/v1/profile/", subject=reader),
        3: wire.exchange("/api/v3/profile", subject=reader, params={"limit": "40"}),
    }


@pytest.fixture
def stock_without_seeded_profile(stock, stock_versions, stock_wire):
    contract, name, _, docs, _ = stock_versions
    _, _, original, _, properties = stock_wire
    [profile] = docs["profile"]
    ident = profile["_id"]
    assert all(ident in original[api, "profile"] for api in (1, 3))
    assert properties["basal"]["current"]["totalbasal"] == profile["store"]["Synthetic"]["basal"][0]["value"]

    with alternate_seed_rows(stock, "profile", docs["profile"], []):
        reader = read_subject(stock, name, contract)
        _await_reading(docs["entries"][-1], reader, usable=True)
        yield reader, ident

    restored_reader = read_subject(stock, name, contract)
    for api, body in _read_stock_profiles(stock_wire[0], restored_reader).items():
        restored = _seed_rows(body, {ident}, api)[ident]
        assert restored["startDate"] == original[api, "profile"][ident]["startDate"]
        original_rate = original[api, "profile"][ident]["store"]["Synthetic"]["basal"]
        assert restored["store"]["Synthetic"]["basal"] == original_rate


async def test_basal_under_missing_seeded_profile_follows_stock(
    hass, stock_without_seeded_profile, stock_reads, monkeypatch,
):
    reader, removed_id = stock_without_seeded_profile
    wire = stock_reads
    profiles = await hass.async_add_executor_job(_read_stock_profiles, wire, reader)
    legacy, modern = profiles[1], v3_result(profiles[3])
    assert isinstance(legacy, list) and isinstance(modern, list)
    legacy_ids = {row["_id"] for row in legacy}
    modern_ids = {row["identifier"] for row in modern}
    assert removed_id not in legacy_ids and removed_id not in modern_ids
    assert len(legacy_ids) == len(legacy) and len(modern_ids) == len(modern)
    assert legacy_ids == modern_ids, "legacy and v3 disagree on the remaining persisted profiles"

    props = await hass.async_add_executor_job(lambda: wire.exchange(PROPERTIES, subject=reader))
    basal = ((props.get("basal") or {}).get("current") or {}).get("totalbasal")
    async with _integration(hass, reader, monkeypatch) as (entry, entities, _):
        _assert_basal_matches_stock(entry.runtime_data.data, _state(hass, entities, "basal"), basal)
