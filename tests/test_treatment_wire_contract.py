"""Check treatment quantities at the read boundary without a Home Assistant runtime.

The Nightscout 15.0.6 v1 and v3 Treatment schemas declare carbs and insulin
as numbers. Only v3 declares duration. The v1 stock write normalizer stores
numeric duration and preserves zero (lib/server/treatments.js), and v1 reads
return that field unchanged (lib/api/treatments/index.js).
"""

import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pytest


spec = importlib.util.spec_from_file_location(
    "ns_treatment_wire_api",
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nightscout_extended"
    / "api.py",
)
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 2, tzinfo=timezone.utc)
EVENT = "Meal Bolus"
NOTES = "Preserve this event"


@pytest.fixture(params=["legacy", "v3"])
def read_treatments(request, monkeypatch):
    def read(fields):
        rows = []
        for hour, values in enumerate(fields, start=1):
            row = {"_id": f"event-{hour}", "eventType": EVENT, "notes": NOTES, **values}
            if request.param == "v3":
                row["date"] = int(START.replace(hour=hour).timestamp() * 1000)
            else:
                row["created_at"] = f"2026-01-01T{hour:02}:00:00.000Z"
            rows.append(row)

        async def exercise():
            client = api.NightscoutClient(None, "https://nightscout.example", None)

            async def get_legacy(path, query):
                assert request.param == "legacy"
                assert path == "/api/v1/treatments.json"
                return rows

            async def get_v3(path, query):
                assert request.param == "v3"
                assert path == "/api/v3/treatments"
                return {"status": 200, "result": rows if "date$gte" in query else []}

            monkeypatch.setattr(client, "_get", get_legacy)
            monkeypatch.setattr(client, "_protected_v3", get_v3)
            # Select the existing client mode. Authentication is outside this contract.
            client._use_v3 = request.param == "v3"
            return await client.async_get_treatments(START, END)

        return asyncio.run(exercise())

    return read


@pytest.mark.parametrize(
    "field, attr, integer, fraction",
    [
        ("carbs", "carbs", 26, 12.75),
        ("insulin", "insulin", 3, 1.25),
        ("duration", "duration_min", 45, 18.5),
    ],
)
def test_finite_wire_numbers_survive(read_treatments, field, attr, integer, fraction):
    treatments = read_treatments([{field: integer}, {field: fraction}])
    assert [getattr(t, attr) for t in treatments] == [integer, fraction]


@pytest.mark.parametrize(
    "field, attr", [("carbs", "carbs"), ("insulin", "insulin"), ("duration", "duration_min")]
)
def test_explicit_zero_is_distinct_from_absence(read_treatments, field, attr):
    zero, absent = read_treatments([{field: 0}, {}])
    value = getattr(zero, attr)
    assert type(value) in (int, float) and value == 0
    assert getattr(absent, attr) is None


@pytest.mark.parametrize(
    "field, attr, siblings",
    [
        ("carbs", "carbs", (("insulin", 1.25), ("duration_min", 18.5))),
        ("insulin", "insulin", (("carbs", 12.75), ("duration_min", 18.5))),
        ("duration", "duration_min", (("carbs", 12.75), ("insulin", 1.25))),
    ],
)
@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param("6.5", id="numeric-string"),
        pytest.param("0", id="zero-string"),
        pytest.param("invalid", id="nonnumeric-string"),
        pytest.param({"value": 6.5}, id="object"),
        pytest.param([6.5], id="array"),
        pytest.param(None, id="null"),
        # Python's JSON reader can produce a nonfinite float on overflow.
        pytest.param(json.loads("1e309"), id="positive-overflow"),
        pytest.param(json.loads("-1e309"), id="negative-overflow"),
        pytest.param(float("nan"), id="nan-at-parser-boundary"),
    ],
)
def test_invalid_field_creates_no_number_and_keeps_event(read_treatments, field, attr, siblings, invalid):
    # Supplier input may be malformed. The integration must avoid inventing a
    # number without dropping an otherwise useful event or its sibling values.
    [event] = read_treatments([{"carbs": 12.75, "insulin": 1.25, "duration": 18.5, field: invalid}])
    assert event.time == START.replace(hour=1)
    assert event.event_type == EVENT
    assert event.notes == NOTES
    for sibling, expected in siblings:
        assert getattr(event, sibling) == expected
    assert getattr(event, attr) is None


@pytest.mark.parametrize("field, prior", [("carbs", 12.75), ("insulin", 1.25)])
def test_zero_and_missing_amount_do_not_replace_last_intake(read_treatments, field, prior):
    treatments = read_treatments([{field: prior}, {field: 0}, {}])
    data = api.NightscoutData(treatments=treatments)
    last = data.last_treatment(field, END)
    assert last is not None
    assert last.time == START.replace(hour=1)
    assert getattr(last, field) == prior
    assert api.NightscoutData(treatments=treatments[1:]).last_treatment(field, END) is None
