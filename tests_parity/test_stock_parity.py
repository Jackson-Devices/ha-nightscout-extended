"""Parity of the integration with a running, pinned stock Nightscout.

Every expected value here comes from Nightscout: the stock server's own responses, JavaScript's own ``Number()``,
or Nightscout's own display functions run from the container's source. A mismatch means the integration differs
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

from .conftest import BASE_URL, HERE, ROOT, approved_exceptions, node, report

MGDL = UnitOfBloodGlucoseConcentration.MILLIGRAMS_PER_DECILITER
MMOL = UnitOfBloodGlucoseConcentration.MILLIMOLE_PER_LITER
CARD = ROOT / "custom_components" / "nightscout_extended" / "www" / "nightscout-glucose-card.js"

# Treatment amounts as they can be stored by uploaders that write to MongoDB directly. The v1 write path would
# normalise them, so they're inserted straight into the database. "__INFINITY__" becomes JavaScript Infinity.
AMOUNTS = [
    12.75, 0, -1, "6.5", " 6.5 ", "0x1A", "1e2", "1_0", "١٢", "Infinity", "", "0", "invalid",
    True, False, None, [6.5], [], [1, 2], {"value": 6.5}, "__INFINITY__",
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ha_unit(site_units: str) -> str:
    return MMOL if str(site_units).lower().startswith("mmol") else MGDL


@pytest.fixture(scope="session")
def seeded(stock):
    """Seed readings and treatments once per server, then wait until Nightscout has processed them."""
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
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        bgnow = stock.get("/api/v2/properties/bgnow").get("bgnow") or {}
        if bgnow.get("mills") == newest:
            break
        time.sleep(3)
    else:
        raise RuntimeError(f"Nightscout never reported the seeded reading in bgnow: {bgnow}")
    return {"now": now, "readings": readings}


async def _setup(hass: HomeAssistant, stock) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=BASE_URL, title="Parity",
        data={CONF_URL: BASE_URL, CONF_API_KEY: stock.secret, CONF_USE_API_SECRET: True},
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
    """On every site, including mmol sites configured in mmol, /api/v1/status serves mg/dL thresholds."""
    thresholds = stock.get("/api/v1/status.json")["settings"]["thresholds"]
    assert all(value >= 30 for value in thresholds.values()), f"thresholds not in mg/dL: {thresholds}"


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


async def test_treatment_amounts_match_javascript(stock, seeded):
    """Nightscout's plugins read an amount as present when it's truthy, with the value `Number(raw)`."""
    now = seeded["now"]
    start, end = now - timedelta(days=1), now + timedelta(hours=1)
    raw = stock.get_raw("/api/v1/treatments.json?" + urlencode({
        "find[created_at][$gte]": _iso(start), "find[created_at][$lte]": _iso(end), "count": "1000",
    })).decode()
    oracle = {row["id"]: row for row in node(HERE / "js" / "js_number.mjs", {"raw": raw, "fields": ["carbs", "insulin"]})}

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
                                   **({"needs_owner_approval": exception} if exception and exception not in approved else {})})
    assert not mismatches, report(mismatches)


async def test_card_shows_what_nightscout_shows(hass: HomeAssistant, stock, seeded, native_lib):
    """For every reading from 40 to 450 mg/dL, the card's number and colour class match Nightscout's display.

    Each case is real integration output: the reading is pushed through the integration's coordinator and the
    resulting Home Assistant states are given to the card.
    """
    settings = stock.get("/api/v1/status.json")["settings"]
    entry = await _setup(hass, stock)
    coordinator = entry.runtime_data
    now = datetime.now(timezone.utc)

    readings = list(range(40, 451))
    cases = []
    for mgdl in readings:
        coordinator.async_set_updated_data(
            dataclasses.replace(coordinator.data, glucose=float(mgdl), glucose_time=now)
        )
        await hass.async_block_till_done()
        cases.append({
            "glucose": hass.states.get("sensor.parity_glucose").as_dict(),
            "last": hass.states.get("sensor.parity_last_reading").as_dict(),
        })

    shown = node(HERE / "js" / "card_render.mjs", {"card": str(CARD), "now": now.isoformat(), "cases": cases})
    native = node(HERE / "js" / "native_display.mjs", {
        "lib": str(native_lib),
        "settings": {"units": settings["units"], "thresholds": settings["thresholds"], "theme": settings.get("theme")},
        "cases": readings,
    })

    level_to_class = {"urgent_low": "urgent", "urgent_high": "urgent", "low": "warning", "high": "warning",
                      "in_range": "inrange"}
    approved = approved_exceptions()
    mismatches = []
    for mgdl, card, ns in zip(readings, shown, native, strict=True):
        if card["text"] != ns["text"]:
            mismatches.append({"mgdl": mgdl, "what": "number", "nightscout": ns["text"], "card": card["text"]})
        card_class = level_to_class.get(card["level"], card["level"])
        if card_class != ns["cls"]:
            if ns["cls"] == "" and "E-1c" not in approved:
                mismatches.append({"mgdl": mgdl, "what": "colour where Nightscout shows none",
                                   "theme": settings.get("theme"), "card": card["level"],
                                   "needs_owner_approval": "E-1c"})
            elif ns["cls"] != "":
                mismatches.append({"mgdl": mgdl, "what": "colour class", "nightscout": ns["cls"],
                                   "card": card["level"]})
    assert not mismatches, report(mismatches)


async def test_card_change_text_matches_nightscout(hass: HomeAssistant, stock, seeded):
    """The card's change text equals Nightscout's own `delta.display` for the seeded readings."""
    settings = stock.get("/api/v1/status.json")["settings"]
    delta = stock.get("/api/v2/properties/delta").get("delta") or {}
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
    assert card["delta"] == delta["display"], report([{
        "what": "change text", "site_units": settings["units"], "nightscout": delta["display"], "card": card["delta"],
    }])
