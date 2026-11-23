"""Compare the corrected Home Assistant product with stock's threshold plugin."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from aiohttp import web
import pytest
import pytest_asyncio
from homeassistant.const import CONF_URL
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from .conftest import HERE, ROOT, node

pytestmark = pytest.mark.corrected_product
CARD = ROOT / "custom_components/nightscout_extended/www/nightscout-glucose-card.js"
DOMAIN = "nightscout_extended"
NOW = datetime(2026, 1, 8, 12, tzinfo=timezone.utc)
FLAGS = {
    "alarmUrgentLow": "urgent_low",
    "alarmLow": "low",
    "alarmHigh": "high",
    "alarmUrgentHigh": "urgent_high",
}
NATIVE_KEYS = {
    "bgLow": "urgent_low",
    "bgTargetBottom": "low",
    "bgTargetTop": "high",
    "bgHigh": "urgent_high",
}
BADGES = {
    "urgent_low": "Urgent low alarm",
    "low": "Low alarm",
    "high": "High alarm",
    "urgent_high": "Urgent high alarm",
}


def _millis(when):
    return int(when.timestamp() * 1000)


def _reading(mgdl, *, age_ms=60_000):
    return {"mgdl": mgdl, "mills": _millis(NOW) - age_ms}


def _oracle(native_lib, cases):
    return node(HERE / "js/native_alarm.mjs", {"lib": str(native_lib), "cases": cases})


def _case(settings, reading, now=NOW):
    return {"settings": deepcopy(settings), "entry": reading, "now": _millis(now)}


@pytest_asyncio.fixture
async def alarm_site(stock):
    """Serve bounded synthetic cases using the real stock status schema and site unit."""
    settings = stock.get("/api/v1/status.json")["settings"]
    assert set(FLAGS) <= settings.keys(), "stock status did not publish all four alarm flags"
    wire = {
        "settings": deepcopy(settings),
        "bgnow": {},
        "entries": [],
        "methods": [],
    }

    async def read(request):
        wire["methods"].append(request.method)
        if request.method != "GET":
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        if request.path == "/api/v1/status.json":
            return web.json_response({"settings": wire["settings"]})
        if request.path.startswith("/api/v2/properties/"):
            return web.json_response({"bgnow": wire["bgnow"]})
        if request.path in {"/api/v1/entries.json", "/api/v1/entries/sgv.json"}:
            return web.json_response(wire["entries"])
        if request.path == "/api/v1/treatments.json":
            return web.json_response([])
        raise web.HTTPNotFound()

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", read)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        wire["url"] = f"http://127.0.0.1:{runner.addresses[0][1]}"
        yield wire
        assert wire["methods"] and set(wire["methods"]) == {"GET"}, "the HA product issued a non-read request"
    finally:
        await runner.cleanup()


def _set_wire(site, reading):
    site["bgnow"] = {
        "last": reading["mgdl"], "mills": reading["mills"],
        "sgvs": [reading],
    }
    site["entries"] = [{"type": "sgv", "sgv": reading["mgdl"], "date": reading["mills"]}]


async def _entry(hass, site, reading, freezer):
    freezer.move_to(NOW)
    _set_wire(site, reading)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=site["url"], title="Parity", data={CONF_URL: site["url"]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id), "corrected product failed entry setup"
    await hass.async_block_till_done()
    return entry


async def _poll(hass, entry, site, reading):
    _set_wire(site, reading)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _configure(hass, entry, **overrides):
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.FORM, "product has no configured alarm options form"
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {"scan_interval": 60, **overrides})
    assert result["type"] is FlowResultType.CREATE_ENTRY, "alarm options did not save"
    await hass.async_block_till_done()


def _observation(hass):
    glucose = hass.states.get("sensor.parity_glucose")
    last = hass.states.get("sensor.parity_last_reading")
    assert glucose is not None and last is not None, "required Home Assistant entities are missing"
    assert "simple_alarm" in glucose.attributes, (
        "missing product capability: glucose has no simple_alarm attribute; "
        "check that pytest loaded the pinned corrected PR1 product"
    )
    return glucose, {"glucose": glucose.as_dict(), "last": last.as_dict()}


def _compare_alarm(hass, expected, reading, *, eligible=True):
    glucose, card_case = _observation(hass)
    # Stock's request sink is silent for both in-range and ineligible input.
    # HA keeps those two states distinguishable in the entity attributes.
    if eligible:
        assert glucose.attributes["simple_alarm"] == expected, f"reading {reading} differs from stock simplealarms"
        expires = datetime.fromisoformat(glucose.attributes["simple_alarm_until"])
        assert _millis(expires) == reading["mills"] + 600_000
    else:
        assert expected == "none", f"stock requested an alarm for ineligible reading {reading}"
        assert glucose.attributes["simple_alarm"] is None
        assert glucose.attributes["simple_alarm_until"] is None
    return card_case


def _synthetic_thresholds(site):
    # 180/181 and 250/251 are distinct raw readings that collapse to equal
    # tenths on a mmol site. The stock JavaScript derives the expected outcomes.
    site["settings"]["thresholds"] = {"bgLow": 52, "bgTargetBottom": 72, "bgTargetTop": 180, "bgHigh": 250}
    site["settings"].update(dict.fromkeys(FLAGS, True))


async def test_default_site_alarms_and_options_round_trip(hass, freezer, alarm_site, native_lib):
    """Read the profile's actual thresholds, then change and independently clear local overrides."""
    site = alarm_site
    reading = _reading(305)
    entry = await _entry(hass, site, reading, freezer)
    stocked = site["settings"]
    original = deepcopy(stocked["thresholds"])
    glucose, first_case = _observation(hass)
    assert glucose.attributes["alarm_thresholds_mgdl"] == {
        name: original[key] for key, name in NATIVE_KEYS.items()
    }
    observed = [(glucose.attributes["simple_alarm"], first_case)]
    cases = [_case(stocked, reading)]
    low = original["bgTargetBottom"] - 1

    for overrides, effective in (
        ({"alarm_urgent_high_mgdl": 300, "alarm_low_mgdl": low}, {"bgHigh": 300, "bgTargetBottom": low}),
        ({"alarm_urgent_high_mgdl": 310, "alarm_low_mgdl": low}, {"bgHigh": 310, "bgTargetBottom": low}),
        ({"alarm_low_mgdl": low}, {"bgTargetBottom": low}),
        ({}, {}),
    ):
        await _configure(hass, entry, **overrides)
        assert entry.options == {"scan_interval": 60, **overrides}
        glucose, card_case = _observation(hass)
        expected_thresholds = {**original, **effective}
        assert glucose.attributes["alarm_thresholds_mgdl"] == {
            name: expected_thresholds[key] for key, name in NATIVE_KEYS.items()
        }
        assert stocked["thresholds"] == original, "site settings were changed by HA options"
        cases.append(_case({**stocked, "thresholds": expected_thresholds}, reading))
        observed.append((glucose.attributes["simple_alarm"], card_case))

    expected = await hass.async_add_executor_job(_oracle, native_lib, cases)
    assert expected[1:3] == ["urgent_high", "high"], "override cases do not distinguish the alarm boundary"
    assert [alarm for alarm, _ in observed] == expected
    cards = await hass.async_add_executor_job(node, HERE / "js/card_render.mjs", {
        "card": str(CARD), "now": NOW.isoformat(), "cases": [case for _, case in observed],
    })
    for alarm, card in zip(expected, cards, strict=True):
        assert card["hasAlarmElement"], "corrected product card has no separate alarm element"
        assert card["alarm"] == BADGES.get(alarm)
        assert BADGES[alarm] in card["ariaLabel"]
    assert len({card["text"] for card in cards}) == 1
    assert cards[0]["text"] == ("16.9" if stocked["units"] == "mmol" else "305")


async def test_strict_boundaries_units_eligibility_and_expiry(hass, freezer, alarm_site, native_lib):
    """Drive real coordinator reads at native strict and rounding boundaries."""
    site = alarm_site
    _synthetic_thresholds(site)
    observations = [
        (_reading(39), False),
        *((_reading(mgdl), True) for mgdl in
          [40, 51, 52, 53, 70, 71, 72, 73, 179, 180, 181, 182, 249, 250, 251, 252, 401]),
        (_reading(401, age_ms=600_001), False),
        (_reading(401, age_ms=600_000), False),
        (_reading(401, age_ms=599_999), True),
    ]
    expected = await hass.async_add_executor_job(_oracle, native_lib, [
        _case(site["settings"], reading) for reading, _ in observations
    ])
    assert set(expected) == {"none", *BADGES}
    assert expected[-3:] == ["none", "none", "urgent_high"]
    entry = await _entry(hass, site, observations[0][0], freezer)
    cases = []
    for (reading, eligible), alarm in zip(observations, expected, strict=True):
        await _poll(hass, entry, site, reading)
        cases.append(_compare_alarm(hass, alarm, reading, eligible=eligible))

    # Keep the active HA state snapshot and show that the card expires it even
    # before it has received a new state from the integration.
    expiry = NOW + timedelta(milliseconds=1)
    cases.append({**cases[-1], "now": expiry.isoformat()})
    cards = await hass.async_add_executor_job(node, HERE / "js/card_render.mjs", {
        "card": str(CARD), "now": NOW.isoformat(), "cases": cases,
    })
    for alarm, card in zip([*expected, "none"], cards, strict=True):
        assert card["hasAlarmElement"]
        assert card["alarm"] == BADGES.get(alarm)
        if badge := BADGES.get(alarm):
            assert badge in card["ariaLabel"]
    assert cards[-2]["text"] == cards[-1]["text"] == ("22.3" if site["settings"]["units"] == "mmol" else "401")

    requests = len(site["methods"])
    freezer.move_to(expiry)
    async_fire_time_changed(hass, expiry)
    await hass.async_block_till_done()
    _compare_alarm(hass, "none", observations[-1][0], eligible=False)
    assert len(site["methods"]) == requests, "alarm expiry caused a request to Nightscout"


async def test_alarm_enable_flags_and_missing_metadata(hass, freezer, alarm_site, native_lib):
    """Each stock enable flag can veto its branch; absent metadata suppresses the new indication."""
    site = alarm_site
    _synthetic_thresholds(site)
    original = deepcopy(site["settings"])
    entry = await _entry(hass, site, _reading(401), freezer)
    _compare_alarm(hass, "urgent_high", _reading(401))
    cases = []
    observed = []
    for flag, mgdl in (
        ("alarmUrgentLow", 40),
        ("alarmLow", 60),
        ("alarmHigh", 200),
        ("alarmUrgentHigh", 401),
    ):
        reading = _reading(mgdl)
        site["settings"] = {**original, flag: False}
        _set_wire(site, reading)
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        glucose, _ = _observation(hass)
        cases.append(_case(site["settings"], reading))
        observed.append(glucose.attributes["simple_alarm"])
    expected = await hass.async_add_executor_job(_oracle, native_lib, cases)
    assert expected == ["low", "none", "none", "high"]
    assert observed == expected

    site["settings"] = {key: value for key, value in original.items() if key != "alarmHigh"}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    glucose, card_case = _observation(hass)
    assert glucose.attributes["simple_alarm"] is None
    assert glucose.attributes["simple_alarm_until"] is None
    assert glucose.state not in {"unknown", "unavailable"}
    [card] = await hass.async_add_executor_job(node, HERE / "js/card_render.mjs", {
        "card": str(CARD), "now": NOW.isoformat(), "cases": [card_case],
    })
    assert card["hasAlarmElement"] and card["alarm"] is None
