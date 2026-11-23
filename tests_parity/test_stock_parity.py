"""Parity of the integration with a running, pinned stock Nightscout.

Every expected value here comes from Nightscout: the stock server's own responses, JavaScript's own ``Number()``,
or Nightscout's own display functions and stylesheet served by the stock container. A mismatch means the integration
differs
from Nightscout. See CONTRIBUTING.md (U-4).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
import json
import re
import time
from urllib.parse import urlencode

import aiohttp
import pytest
from homeassistant.const import CONF_API_KEY, CONF_URL, UnitOfBloodGlucoseConcentration
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import BloodGlucoseConcentrationConverter
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nightscout_extended.api import NightscoutClient
from custom_components.nightscout_extended.const import CONF_USE_API_SECRET, DOMAIN

from .conftest import BASE_URL, HERE, ROOT, approved_exceptions, node, note_exception, report

MGDL = UnitOfBloodGlucoseConcentration.MILLIGRAMS_PER_DECILITER
MMOL = UnitOfBloodGlucoseConcentration.MILLIMOLE_PER_LITER
CARD = ROOT / "custom_components" / "nightscout_extended" / "www" / "nightscout-glucose-card.js"

# Treatment amounts as they can be stored by uploaders that write to MongoDB directly. The v1 write path would
# normalise them, so they're inserted straight into the database. "__INFINITY__" becomes JavaScript Infinity.
# Readings a tenth either side of every profile's thresholds (mg/dL and the mmol profile's, in mg/dL).
FRACTIONAL_READINGS = sorted({round(edge + step, 1) for edge in (55, 60, 63, 72, 75, 80, 170, 180, 250, 252, 260)
                              for step in (-0.1, 0.1)})
AMOUNTS = [
    12.75, 0, -1, "6.5", " 6.5 ", "0x1A", "1e2", "1_0", "١٢", "Infinity", "", "0", "invalid",
    True, False, None, [6.5], [], [1, 2], {"value": 6.5}, "__INFINITY__",
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ha_unit(site_units: str) -> str:
    return MMOL if str(site_units).lower().startswith("mmol") else MGDL


@pytest.fixture(scope="session")
def stock_seed_result():
    """Keep the seed result across tests in this disposable stock profile."""
    return {}


@pytest.fixture
def seeded(stock, allow_local_stock_server, stock_seed_result):
    """Initialize stock once, after this test's loopback socket guard is active."""
    if "result" in stock_seed_result:
        assert stock_seed_result["result"] is not None, (
            "the prior stock seed attempt failed; restart the disposable site"
        )
        return stock_seed_result["result"]
    # A failure after a write may have seeded only part of the data.
    stock_seed_result["result"] = None

    now = datetime.now(timezone.utc).replace(microsecond=0)
    readings = [
        # An entry labelled mmol/L, older than the valid readings so it can't be the current one.
        {"type": "sgv", "sgv": 10, "units": "mmol/L", "date": int((now - timedelta(minutes=16)).timestamp() * 1000),
         "direction": "Flat", "device": "parity"},
        {"type": "sgv", "sgv": 150, "date": int((now - timedelta(minutes=11)).timestamp() * 1000),
         "direction": "Flat", "device": "parity"},
        {"type": "sgv", "sgv": 155, "date": int((now - timedelta(minutes=6)).timestamp() * 1000),
         "direction": "FortyFiveUp", "device": "parity"},
    ]
    for reading in readings:
        reading["dateString"] = _iso(datetime.fromtimestamp(reading["date"] / 1000, timezone.utc))
    stock.post("/api/v1/entries", readings)

    docs = []
    for index, amount in enumerate(AMOUNTS):
        docs.append({
            "eventType": "Meal Bolus", "enteredBy": "parity", "notes": f"parity-{index}",
            "created_at": _iso(now - timedelta(hours=3) + timedelta(minutes=2 * index)),
            "carbs": amount, "insulin": amount,
        })
    docs.append({"eventType": "Note", "enteredBy": "parity", "notes": "parity-missing",
                 "created_at": _iso(now - timedelta(minutes=30))})
    stock.mongo(
        "var docs = " + json.dumps(docs) + ";\n"
        "docs.forEach(function (d) { ['carbs', 'insulin'].forEach(function (k) {"
        " if (d[k] === '__INFINITY__') d[k] = Infinity; }); });\n"
        "db.treatments.insertMany(docs);\n"
    )

    newest = readings[-1]["date"]
    _wait_for_bgnow(stock, lambda bgnow: bgnow.get("mills") == newest, "report the seeded reading")
    result = {"now": now, "readings": readings}
    stock_seed_result["result"] = result
    return result


def _wait_for_bgnow(stock, matches, operation):
    deadline = time.monotonic() + 150
    while True:
        bgnow = stock.get("/api/v2/properties/bgnow").get("bgnow") or {}
        if matches(bgnow):
            return bgnow
        if time.monotonic() >= deadline:
            pytest.fail(f"Nightscout did not {operation}: {bgnow}")
        time.sleep(3)


@pytest.fixture
def invalid_latest_entry(stock, seeded):
    """Wait for the exact CGM error in bgnow, then remove it through stock's HTTP storage path."""
    baseline = seeded["readings"][-1]["date"]
    baseline_bgnow = _wait_for_bgnow(stock, lambda bgnow: bgnow.get("mills") == baseline, "report the baseline")
    [baseline_entry] = stock.get("/api/v1/entries.json?count=1")
    assert baseline_entry["date"] == baseline

    # Five minutes beyond the latest normal reading places the error in its own stock bucket.
    newest = baseline + 5 * 60 * 1000
    device = "parity-invalid-mmol"
    case = {
        "type": "sgv", "sgv": 10, "units": "mmol/L", "date": newest, "direction": "Flat", "device": device,
        "dateString": _iso(datetime.fromtimestamp(newest / 1000, timezone.utc)),
    }
    query = urlencode({"find[date]": newest, "find[device]": device, "count": 2})
    case_path = "/api/v1/entries.json?" + query

    def entries_for_case():
        entries = stock.get(case_path)
        assert all(entry["device"] == device and entry["date"] == newest for entry in entries)
        return entries

    assert not entries_for_case(), "the invalid test entry was already present"

    try:
        stock.post("/api/v1/entries", [case])
        [written] = entries_for_case()
        [latest] = stock.get("/api/v1/entries.json?count=1")
        assert latest["_id"] == written["_id"]
        assert written["sgv"] == case["sgv"] and written["units"] == case["units"]
        yield _wait_for_bgnow(
            stock,
            lambda bgnow: any(
                error.get("mills") == newest and error.get("mgdl") == case["sgv"]
                for error in bgnow.get("errors", [])
            ),
            "report the exact newest entry as a CGM error",
        )
    finally:
        for entry in entries_for_case():
            entry_id = entry["_id"]
            assert re.fullmatch("[0-9a-fA-F]{24}", entry_id), "stock did not return a MongoDB entry ID"
            stock.delete(f"/api/v1/entries/{entry_id}")
        assert not entries_for_case(), "the invalid test entry survived cleanup"
        [restored] = stock.get("/api/v1/entries.json?count=1")
        assert restored["_id"] == baseline_entry["_id"], "stock latest raw entry was not restored"
        _wait_for_bgnow(stock, lambda bgnow: bgnow == baseline_bgnow, "restore bgnow after the invalid case")


async def _setup(hass: HomeAssistant, stock, *, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=BASE_URL, title="Parity",
        data={CONF_URL: BASE_URL, CONF_API_KEY: stock.secret, CONF_USE_API_SECRET: True},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id), "integration failed to set up against stock"
    await hass.async_block_till_done()
    return entry


# ---- Nightscout's own behaviour (keeps the assumptions in these tests honest) ------------------------------


def test_stock_stores_entries_as_sent_and_reads_them_as_mgdl(stock, seeded):
    """Entries are stored unconverted; bgnow is in mg/dL on every site; only `scaled` follows the site unit."""
    entries = stock.get("/api/v1/entries.json?" + urlencode({"count": "10"}))
    labelled = [e for e in entries if e.get("units") == "mmol/L"]
    assert labelled and labelled[0]["sgv"] == 10, f"stock changed a stored mmol-labelled entry: {labelled}"

    bgnow = stock.get("/api/v2/properties/bgnow")["bgnow"]
    assert bgnow["last"] == 155, f"bgnow.last is not the mg/dL reading: {bgnow}"


def test_stock_serves_thresholds_in_mgdl(stock):
    """On every site, including mmol sites configured in mmol, /api/v1/status serves mg/dL thresholds.

    15.0.6 lib/settings.js converts all four from mmol when bgHigh < 50; a configured 0 (BG_LOW=0, as in the
    mgdl-custom profile) stays 0, so the three target and high limits carry the unit check.
    """
    thresholds = stock.get("/api/v1/status.json")["settings"]["thresholds"]
    limits = [thresholds[name] for name in ("bgHigh", "bgTargetTop", "bgTargetBottom")]
    assert all(value >= 30 for value in limits), f"thresholds not in mg/dL: {thresholds}"
    assert thresholds["bgLow"] == 0 or thresholds["bgLow"] >= 30, f"bgLow not in mg/dL: {thresholds}"
    if stock.profile == "mgdl-custom":
        assert thresholds["bgLow"] == 0, f"the BG_LOW=0 profile did not serve 0: {thresholds}"


# ---- the integration against stock --------------------------------------------------------------------------


async def test_units_and_thresholds_are_the_served_values(hass: HomeAssistant, stock, seeded):
    settings = stock.get("/api/v1/status.json")["settings"]
    await _setup(hass, stock)
    glucose = hass.states.get("sensor.parity_glucose")
    assert glucose is not None, "glucose sensor missing"

    mismatches = []
    if glucose.attributes.get("unit_of_measurement") != _ha_unit(settings["units"]):
        mismatches.append({"what": "unit", "site": settings["units"],
                           "integration": glucose.attributes.get("unit_of_measurement")})
    served = settings["thresholds"]
    expected = {"urgent_low": served["bgLow"], "low": served["bgTargetBottom"],
                "high": served["bgTargetTop"], "urgent_high": served["bgHigh"]}
    actual = glucose.attributes.get("thresholds_mgdl")
    if actual != {k: float(v) for k, v in expected.items()}:
        mismatches.append({"what": "thresholds", "served": served, "integration": actual})
    assert not mismatches, report(mismatches)


async def test_glucose_change_trend_and_time_match_bgnow(hass: HomeAssistant, stock, seeded):
    props = stock.get("/api/v2/properties/bgnow,delta,direction")
    unit = _ha_unit(stock.get("/api/v1/status.json")["settings"]["units"])
    to_unit = BloodGlucoseConcentrationConverter.converter_factory(MGDL, unit)
    await _setup(hass, stock)

    mismatches = []
    bgnow, delta, direction = props.get("bgnow") or {}, props.get("delta") or {}, props.get("direction") or {}

    def compare(what, entity_id, native_mgdl):
        state = hass.states.get(entity_id)
        if native_mgdl is None:
            if state.state not in ("unknown", "unavailable"):
                mismatches.append({"what": what, "stock": None, "integration": state.state})
            return
        expected = to_unit(float(native_mgdl))
        try:
            ok = abs(float(state.state) - expected) <= 1e-9 * max(1.0, abs(expected))
        except ValueError:
            ok = False
        if not ok:
            mismatches.append({"what": what, "stock_mgdl": native_mgdl, "expected_state": expected,
                               "integration": state.state})

    compare("glucose", "sensor.parity_glucose", bgnow.get("last"))
    compare("change", "sensor.parity_glucose_change", delta.get("mgdl"))

    reading_time = hass.states.get("sensor.parity_last_reading").state
    expected_time = datetime.fromtimestamp(bgnow["mills"] / 1000, timezone.utc) if "mills" in bgnow else None
    if (expected_time is None) != (reading_time in ("unknown", "unavailable")) or (
        expected_time is not None and datetime.fromisoformat(reading_time) != expected_time
    ):
        mismatches.append({"what": "reading time", "stock": bgnow.get("mills"), "integration": reading_time})

    trend = hass.states.get("sensor.parity_glucose_trend").state
    native = direction.get("value")
    expected_trend = re.sub(r"[^a-z0-9]+", "_", re.sub(r"(?<=[a-z])(?=[A-Z])", "_", native).lower()).strip("_") \
        if native else "unknown"
    if trend != expected_trend:
        mismatches.append({"what": "trend", "stock": native, "integration": trend})
    assert not mismatches, report(mismatches)


@pytest.fixture
def stock_treatments(stock, seeded):
    now = seeded["now"]
    start, end = now - timedelta(days=1), now + timedelta(hours=1)
    raw = stock.get_raw("/api/v1/treatments.json?" + urlencode({
        "find[created_at][$gte]": _iso(start), "find[created_at][$lte]": _iso(end), "count": "1000",
    })).decode()
    rows = json.loads(raw)
    expected_notes = {f"parity-{index}" for index in range(len(AMOUNTS))} | {"parity-missing"}
    observed_notes = [row.get("notes") for row in rows if row.get("enteredBy") == "parity"]
    assert set(observed_notes) == expected_notes and len(observed_notes) == len(expected_notes), (
        "stock did not return each seeded treatment exactly once"
    )
    oracle = node(HERE / "js" / "js_number.mjs", {"raw": raw, "fields": ["carbs", "insulin"]})
    by_id = {row["id"]: row for row in oracle}
    assert len(by_id) == len(rows) and all(row["_id"] in by_id for row in rows)
    return start, end, rows, by_id


async def test_treatment_amounts_match_javascript(stock, stock_treatments):
    """Nightscout's plugins read an amount as present when it's truthy, with the value `Number(raw)`."""
    start, end, _, oracle = stock_treatments
    async with aiohttp.ClientSession() as session:
        client = NightscoutClient(session, BASE_URL, stock.secret)
        client.set_uses_api_secret(True)
        treatments = {t.uid: t for t in await client.async_get_treatments(start, end)}

    approved = approved_exceptions()
    mismatches = []
    for uid, native in oracle.items():
        treatment = treatments.get(uid)
        if treatment is None:
            mismatches.append({"id": uid, "what": "treatment missing from the integration"})
            continue
        for field in ("carbs", "insulin"):
            truthy, number = native[field]["truthy"], native[field]["number"]
            shown = getattr(treatment, field) or None  # what any visible surface shows
            if not truthy or number == 0:
                expected, exception = None, None
            elif isinstance(number, str):  # NaN or +/-Infinity: Home Assistant can't hold these
                expected, exception = None, "E-3a"
            else:
                expected, exception = float(number), None
            if shown != expected or (exception and exception not in approved):
                mismatches.append({"id": uid, "field": field, "notes": treatment.notes,
                                   "javascript": native[field], "integration": shown,
                                   **({"needs_owner_approval": exception}
                                      if exception and exception not in approved else {})})
    assert not mismatches, report(mismatches)


async def test_last_treatment_entities_match_stock_amounts(hass: HomeAssistant, stock, seeded, stock_treatments):
    """Read the actual HA intake sensors, including the identity metadata shown with the amount."""
    _, _, rows, oracle = stock_treatments
    await _setup(hass, stock)
    mismatches = []
    for field, unit in (("carbs", "g"), ("insulin", "U")):
        candidates = []
        for row in rows:
            value = oracle[row["_id"]][field]
            if not value["truthy"] or isinstance(value["number"], str) or value["number"] == 0:
                continue
            when = datetime.fromisoformat(row["created_at"])
            if when <= seeded["now"]:
                candidates.append((when, row, value["number"]))
        assert candidates, f"stock provided no finite nonzero {field} amount"
        when, latest, expected = max(candidates, key=lambda case: case[0])
        state = hass.states.get(f"sensor.parity_last_{field}")
        assert state is not None, f"last {field} sensor missing"
        expected_attributes = {"time": when.isoformat(), "event_type": latest["eventType"],
                               "notes": latest["notes"], "unit_of_measurement": unit}
        actual_attributes = {key: state.attributes.get(key) for key in expected_attributes}
        try:
            value_matches = float(state.state) == pytest.approx(float(expected))
        except ValueError:
            value_matches = False
        if not value_matches or actual_attributes != expected_attributes:
            mismatches.append({"what": f"last {field} sensor", "expected": expected, "integration": state.state,
                               "stock_attributes": expected_attributes, "integration_attributes": actual_attributes})
    assert not mismatches, report(mismatches)


# The fixed observation time for rendered cases: midday, so the card's night mode never applies.
def _render_now(seeded) -> datetime:
    return seeded["now"].replace(hour=12, minute=0, second=0)


async def _render_card_cases(hass, stock, seeded, readings, ages_ms=None):
    """Push each reading through the integration's coordinator and render the card from the resulting states.

    `ages_ms[i]` places reading i that many milliseconds before the observation time (default: taken now).
    """
    entry = await _setup(hass, stock)
    coordinator = entry.runtime_data
    now = _render_now(seeded)
    cases = []
    for index, mgdl in enumerate(readings):
        when = now - timedelta(milliseconds=ages_ms[index] if ages_ms else 0)
        coordinator.async_set_updated_data(
            dataclasses.replace(coordinator.data, glucose=float(mgdl), glucose_time=when, cgm_error_code=None)
            if hasattr(coordinator.data, "cgm_error_code")
            else dataclasses.replace(coordinator.data, glucose=float(mgdl), glucose_time=when)
        )
        await hass.async_block_till_done()
        glucose = hass.states.get("sensor.parity_glucose")
        last = hass.states.get("sensor.parity_last_reading")
        assert glucose is not None and last is not None, (
            "card requires the integration's glucose and timestamp entities"
        )
        source = glucose.attributes.get("source_time")
        shown_time = datetime.fromisoformat(source) if source else datetime.fromisoformat(last.state)
        assert abs(shown_time - when) < timedelta(seconds=1), "the card received the previous reading's timestamp"
        cases.append({"glucose": glucose.as_dict(), "last": last.as_dict()})
    return node(HERE / "js" / "card_render.mjs", {"card": str(CARD), "now": now.isoformat(), "cases": cases})


def _native_display(stock, native_lib, readings, *, paint=False, now=None):
    """Nightscout's own current-reading display (updateCurrentSGV) for each reading, with its real timeago.

    A reading is a number (taken at `now`) or {"mgdl": ..., "ageMs": ...}.
    """
    settings = stock.get("/api/v1/status.json")["settings"]
    payload = {
        "lib": str(native_lib),
        # The served settings as Nightscout's client receives them: units, thresholds, theme, Time Ago alarms.
        "settings": settings,
        "cases": readings,
    }
    if now is not None:
        payload["now"] = now.isoformat()
    if paint:
        payload["css"] = stock.get_raw("/css/main.css").decode("utf-8")
    return node(HERE / "js" / "native_display.mjs", payload)


def _css_colour(value):
    """Normalize the pinned stylesheet and card's CSS colour spellings."""
    if value is None:
        return None
    value = value.strip().lower()
    names = {"black": "#000000", "red": "#ff0000", "white": "#ffffff", "yellow": "#ffff00"}
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        return "#" + "".join(digit * 2 for digit in value[1:])
    return names.get(value, value)


def _compare_current_text(mgdl, card, ns, approved):
    """Keep the stock label and HA numeric requirement as independent assertions."""
    if mgdl == 39 or mgdl > 400:
        stock_label = "LOW" if mgdl < 40 else "HIGH"
        if ns["text"] != stock_label:
            return {"mgdl": mgdl, "what": "native presentation label", "expected": stock_label,
                    "nightscout": ns["text"]}
        if "E-1d" not in approved:
            return {"mgdl": mgdl, "what": "numeric presentation", "needs_owner_approval": "E-1d"}
        expected = ns["numericText"]
        if card["text"] == expected:
            note_exception("E-1d", {"mgdl": mgdl, "nightscout": ns["text"], "current": card["text"]})
    else:
        expected = ns["text"]
    if card["text"] != expected:
        return {"mgdl": mgdl, "what": "number", "nightscout": ns["text"], "expected": expected, "card": card["text"]}
    return None


async def test_card_shows_what_nightscout_shows(hass: HomeAssistant, stock, seeded, native_lib):
    """Compare text and both rendered colours from 40 to 450 mg/dL in the default daytime card configuration.

    Stock stores sgv as `Number(sgv)`, so fractional readings either side of each threshold are included.
    """
    readings = list(range(40, 451)) + FRACTIONAL_READINGS
    shown = await _render_card_cases(hass, stock, seeded, readings)
    native = _native_display(stock, native_lib, readings, paint=True, now=_render_now(seeded))

    approved = approved_exceptions()
    mismatches = []
    for mgdl, card, ns in zip(readings, shown, native, strict=True):
        assert ns["mgdl"] == mgdl, "stock display returned a different case"
        if mismatch := _compare_current_text(mgdl, card, ns, approved):
            mismatches.append(mismatch)
        expected = {name: _css_colour(colour) for name, colour in ns["paint"].items()}
        actual = {name: _css_colour(colour) for name, colour in card["paint"].items()}
        if actual == expected:
            continue
        if not ns["rangeClass"]:
            if "E-1c" not in approved:
                mismatches.append({"mgdl": mgdl, "what": "no-range paint", "nightscout": expected, "card": actual,
                                   "needs_owner_approval": "E-1c"})
        else:
            mismatches.append({"mgdl": mgdl, "what": "range paint", "range": ns["rangeClass"],
                               "nightscout": expected, "card": actual})
    assert not mismatches, report(mismatches)


async def test_card_display_limits_match_nightscout(hass: HomeAssistant, stock, seeded, native_lib):
    """Compare the presentation boundaries without using range colour or alarm expectations."""
    readings = [39, 40, 40.5, 399.9, 400, 400.1, 401]
    shown = await _render_card_cases(hass, stock, seeded, readings)
    native = _native_display(stock, native_lib, readings, now=_render_now(seeded))
    approved = approved_exceptions()
    mismatches = []
    for mgdl, card, ns in zip(readings, shown, native, strict=True):
        assert ns["mgdl"] == mgdl, "stock display returned a different case"
        if mismatch := _compare_current_text(mgdl, card, ns, approved):
            mismatches.append(mismatch)
    assert not mismatches, report(mismatches)


async def test_latest_unusable_stock_row_suppresses_older_alarm(hass: HomeAssistant, stock, seeded, native_lib):
    """Use the latest numeric 39 in HA even when stock bgnow omits it from last."""
    query = urlencode({"find[device]": "parity-alarm-latest", "count": 10})
    collection = "/api/v1/entries.json?" + query
    older = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    now = older + timedelta(minutes=2)
    sources = [
        {"type": "sgv", "sgv": mgdl, "date": int(when.timestamp() * 1000), "dateString": _iso(when),
         "direction": "Flat", "device": "parity-alarm-latest"}
        for mgdl, when in [(155, older), (39, older + timedelta(minutes=1))]
    ]
    assert not stock.get(collection)
    try:
        stock.post("/api/v1/entries", [sources[0]])
        _wait_for_bgnow(stock, lambda bg: bg.get("mills") == sources[0]["date"], "report the alarm sample")
        entry = await _setup(hass, stock, options={"alarm_high_mgdl": 120})
        before = hass.states.get("sensor.parity_glucose")
        assert before is not None and "simple_alarm" in before.attributes, (
            "the product under test has no alarm capability"
        )

        stock.post("/api/v1/entries", [sources[1]])
        bgnow = _wait_for_bgnow(
            stock,
            lambda bg: any(row.get("mills") == sources[1]["date"] for row in bg.get("sgvs", [])),
            "report the newer low-limit reading",
        )
        assert bgnow["last"] == sources[0]["sgv"] and bgnow["mills"] == sources[0]["date"]
        raw_cases = sorted(bgnow["sgvs"], key=lambda row: row["mills"])
        assert [row["mgdl"] for row in raw_cases[-2:]] == [155, 39]

        settings = stock.get("/api/v1/status.json")["settings"]
        settings["thresholds"]["bgTargetTop"] = 120  # Apply the local override to the reference input only.
        expected = await hass.async_add_executor_job(node, HERE / "js" / "native_alarm.mjs", {
            "lib": str(native_lib),
            "cases": [{"settings": settings, "entry": row, "now": int(now.timestamp() * 1000)}
                      for row in raw_cases[-2:]],
        })
        assert expected == ["high", "none"]
        assert before.attributes["simple_alarm"] == expected[0]

        [native_display] = _native_display(stock, native_lib, [39])
        assert native_display["text"] == "LOW"
        latest_time = datetime.fromtimestamp(sources[1]["date"] / 1000, timezone.utc)
        renders = []
        for remove_previous in (False, True):
            if remove_previous:
                prior_id = next(row["_id"] for row in stock.get(collection) if row["date"] == sources[0]["date"])
                assert re.fullmatch("[0-9a-fA-F]{24}", prior_id), "stock did not return a MongoDB entry ID"
                stock.delete(f"/api/v1/entries/{prior_id}")
                sole = _wait_for_bgnow(
                    stock,
                    lambda bg: [row["mills"] for row in bg.get("sgvs", [])] == [sources[1]["date"]],
                    "report a bucket with just the newest low-limit row",
                )
                assert sole.get("last") is None and sole.get("mills") is None

            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
            after = hass.states.get("sensor.parity_glucose")
            last = hass.states.get("sensor.parity_last_reading")
            assert after is not None and last is not None
            assert after.attributes["native_mgdl"] == 39
            assert datetime.fromisoformat(after.attributes["source_time"]) == latest_time
            assert datetime.fromisoformat(last.state) == latest_time
            assert after.attributes["simple_alarm"] is None
            assert after.attributes["simple_alarm_until"] is None
            renders.append({"glucose": after.as_dict(), "last": last.as_dict()})

        cards = await hass.async_add_executor_job(node, HERE / "js" / "card_render.mjs", {
            "card": str(CARD), "now": now.isoformat(), "cases": renders,
        })
        assert len(cards) == len(renders)
        for card in cards:
            assert card["text"] == native_display["numericText"]
            assert card["hasAlarmElement"] and card["alarm"] is None
    finally:
        for row in stock.get(collection):
            entry_id = row["_id"]
            assert re.fullmatch("[0-9a-fA-F]{24}", entry_id), "stock did not return a MongoDB entry ID"
            stock.delete(f"/api/v1/entries/{entry_id}")
        assert not stock.get(collection)


async def test_card_change_text_matches_nightscout(hass: HomeAssistant, stock, seeded):
    """The card's change text equals Nightscout's own `delta.display` for the seeded readings."""
    settings = stock.get("/api/v1/status.json")["settings"]
    # Stock briefly serves an empty delta while it reloads its data; wait for a computed one.
    deadline = time.monotonic() + 90
    while not (delta := stock.get("/api/v2/properties/delta").get("delta") or {}).get("display"):
        if time.monotonic() >= deadline:
            break
        time.sleep(3)
    await _setup(hass, stock)
    case = {
        "glucose": hass.states.get("sensor.parity_glucose").as_dict(),
        "last": hass.states.get("sensor.parity_last_reading").as_dict(),
        "delta": hass.states.get("sensor.parity_glucose_change").as_dict(),
    }
    # Render as of one minute after the reading, so the card treats it as current however long the suite ran.
    now = datetime.fromisoformat(case["last"]["state"]) + timedelta(minutes=1)
    [card] = node(HERE / "js" / "card_render.mjs", {"card": str(CARD), "now": now.isoformat(), "cases": [case]})
    assert "display" in delta, f"stock sent no delta.display for the seeded readings: {delta}"
    bgnow = stock.get("/api/v2/properties/bgnow").get("bgnow") or {}
    sent = case["delta"]["attributes"]
    inputs = {"site_display": delta["display"], "recent_mean_mgdl": bgnow.get("mean"),
              "previous_mean_mgdl": delta.get("mean5MinsAgo")}
    wrong = [{"what": "change input", "attribute": name, "nightscout": value, "integration": sent.get(name)}
             for name, value in inputs.items() if sent.get(name) != value]
    assert not wrong, report(wrong)
    assert card["delta"] == delta["display"], report([{
        "what": "change text", "site_units": settings["units"], "nightscout": delta["display"], "card": card["delta"],
    }])


async def test_newest_entry_labelled_mmol_is_not_a_reading(hass: HomeAssistant, stock, invalid_latest_entry):
    """A raw 10 mg/dL CGM error cannot become a 10 mmol/L Home Assistant reading."""
    bgnow = invalid_latest_entry
    assert bgnow.get("last") is None, f"stock's bucket containing the CGM error reported a glucose reading: {bgnow}"

    await _setup(hass, stock)
    state = hass.states.get("sensor.parity_glucose")
    assert state is not None, "glucose sensor missing"
    assert state.state in ("unknown", "unavailable"), report([{
        "what": "newest mmol-labelled entry", "stock_bgnow": bgnow, "integration": state.state,
        "note": "Nightscout shows no current reading here; the integration shows one",
    }])


async def test_card_freshness_matches_nightscout_timeago(hass: HomeAssistant, stock, seeded, native_lib):
    """Either side of the site's Time Ago limits, the card is current exactly when Nightscout's timeago says so.

    Nightscout drops the range colour once timeago.checkStatus is not "current" (adjustCurrentSGVClasses), so the
    stale state and the rendered colours are both compared, 1 ms before, at and 1 ms after each enabled limit.
    """
    settings = stock.get("/api/v1/status.json")["settings"]
    # timeago.checkStatus: `mins = settings.alarmTimeago<Level>Mins || default`, then JS arithmetic. Environment
    # overrides are served as strings (ALARM_TIMEAGO_WARN_MINS=20 serves "20"), defaults as numbers.
    defaults = {"Warn": 15, "Urgent": 30}
    limits = [float(settings.get(f"alarmTimeago{level}Mins") or defaults[level]) for level in defaults
              if settings.get(f"alarmTimeago{level}")]
    assert limits, f"profile {stock.profile} has no Time Ago limit to test: {settings}"
    ages = sorted({int(minutes * 60000) + offset for minutes in limits for offset in (-1, 0, 1)})
    readings = [120] * len(ages)
    shown = await _render_card_cases(hass, stock, seeded, readings, ages_ms=ages)
    native = _native_display(stock, native_lib, [{"mgdl": 120, "ageMs": age} for age in ages], paint=True,
                             now=_render_now(seeded))
    mismatches = []
    for age, card, ns in zip(ages, shown, native, strict=True):
        card_current = not str(card["ariaLabel"] or "").startswith("No recent")
        if card_current != (ns["status"] == "current"):
            mismatches.append({"what": "freshness", "age_ms": age, "nightscout": ns["status"],
                               "card": "current" if card_current else "stale"})
        expected = {name: _css_colour(colour) for name, colour in ns["paint"].items()}
        actual = {name: _css_colour(colour) for name, colour in card["paint"].items()}
        if actual != expected:
            mismatches.append({"what": "freshness paint", "age_ms": age, "nightscout": expected, "card": actual})
    assert not mismatches, report(mismatches)


async def test_cgm_error_codes_show_what_nightscout_shows(hass: HomeAssistant, stock, seeded, native_lib):
    """A newest raw CGM error code is what Nightscout shows (updateCurrentSGV), until a real reading follows it.

    9 shows an empty value (hourglass), other codes below 39 show errorcodes.toDisplay, and the next valid
    reading shows its number again. The glucose sensor must not report a reading while an error is newest.
    """
    device = "parity-error-codes"
    collection = "/api/v1/entries.json?" + urlencode({"find[device]": device, "count": 20})
    assert not stock.get(collection), "error-code test entries were already present"
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=4)
    sequence = [9, 10, 38, 1, 120]  # 120: recovery to a valid reading
    entry = None
    mismatches = []
    try:
        for index, mgdl in enumerate(sequence):
            when = start + timedelta(seconds=50 * index)
            row = {"type": "sgv", "sgv": mgdl, "date": int(when.timestamp() * 1000), "dateString": _iso(when),
                   "direction": "Flat", "device": device}
            stock.post("/api/v1/entries", [row])
            _wait_for_bgnow(
                stock,
                lambda bg: any(r.get("mills") == row["date"] for r in bg.get("sgvs", []) + bg.get("errors", []))
                and (mgdl < 39 or bg.get("mills") == row["date"]),
                f"report the newest entry ({mgdl})",
            )
            if entry is None:
                entry = await _setup(hass, stock)
            else:
                await entry.runtime_data.async_refresh()
                await hass.async_block_till_done()
            glucose = hass.states.get("sensor.parity_glucose")
            last = hass.states.get("sensor.parity_last_reading")
            assert glucose is not None and last is not None
            [ns] = _native_display(stock, native_lib, [mgdl])
            [card] = node(HERE / "js" / "card_render.mjs", {
                "card": str(CARD), "now": (when + timedelta(seconds=30)).isoformat(),
                "cases": [{"glucose": glucose.as_dict(), "last": last.as_dict()}],
            })
            if card["text"] != ns["text"]:
                mismatches.append({"what": "error-code text" if mgdl < 39 else "recovery text", "mgdl": mgdl,
                                   "nightscout": ns["text"], "card": card["text"]})
            if mgdl < 39 and glucose.state not in ("unknown", "unavailable"):
                mismatches.append({"what": "error code reported as a reading", "mgdl": mgdl,
                                   "integration": glucose.state})
            if mgdl >= 39 and glucose.state in ("unknown", "unavailable"):
                mismatches.append({"what": "no reading after recovery", "mgdl": mgdl, "integration": glucose.state})
    finally:
        for row in stock.get(collection):
            entry_id = row["_id"]
            assert re.fullmatch("[0-9a-fA-F]{24}", entry_id), "stock did not return a MongoDB entry ID"
            stock.delete(f"/api/v1/entries/{entry_id}")
        assert not stock.get(collection)
        baseline = seeded["readings"][-1]["date"]
        _wait_for_bgnow(stock, lambda bg: bg.get("mills") == baseline, "restore bgnow after the error-code case")
    assert not mismatches, report(mismatches)


def test_card_change_text_grid_matches_nightscout(native_lib):
    """The card's change text equals Nightscout's bgnow.calcDelta display for every reading pair in the grid.

    Recent 40-400 mg/dL against every previous within 29 mg/dL, on mg/dL and mmol sites, shown in both units.
    """
    result = node(HERE / "js" / "delta_grid.mjs", {
        "lib": str(native_lib), "card": str(CARD), "low": 40, "high": 400, "maxStep": 29,
    })
    assert result["checked"] == 361 * 59 * 4, f"the grid did not cover every case: {result['checked']}"
    assert result["mismatchCount"] == 0, (
        f"{result['mismatchCount']} of {result['checked']} change texts differ from Nightscout\n"
        + report(result["mismatches"])
    )
