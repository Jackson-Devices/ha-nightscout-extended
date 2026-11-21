"""Check native maintenance and AAPS reservoir inputs on disposable Nightscout."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


class SignalCheckError(Exception):
    """A value-free product assertion for the signal smoke."""


def require(value, case):
    if not value:
        raise SignalCheckError(case)


def instant(when):
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seed_signals(anchor, spec):
    age_events = [*spec["age_hours"].items(), ("Insulin Change", spec["older_insulin_hours"])]
    treatments = []
    for label, hours in age_events:
        recorded = anchor - timedelta(hours=hours)
        treatments.append({
            "document": {
                "eventType": label, "created_at": instant(recorded), "date": int(recorded.timestamp() * 1000),
            },
        })
    device_status = []
    for row in spec["device_status"]:
        uploaded = anchor - timedelta(minutes=row["upload_minutes_ago"])
        contact = anchor - timedelta(minutes=row["contact_minutes_ago"])
        document = {
            "device": row["uploader"], "created_at": instant(uploaded),
            "pump": {"reservoir": row["units"], "status": {"timestamp": instant(contact)}},
        }
        if row["native_date"]:
            document["date"] = int(uploaded.timestamp() * 1000)
        device_status.append(document)
    return treatments, device_status


def trace_signals(protected):
    from aiohttp import TraceConfig

    observed = set()
    trace = TraceConfig()

    async def collect(_session, _context, params):
        if params.method != "GET" or params.response.status != 200:
            return
        path, query = params.url.path.rstrip("/"), params.url.query
        if protected:
            if not params.headers.get("Authorization", "").startswith("Bearer "):
                return
            if path == "/api/v3/treatments" and "eventType$in" in query:
                observed.add("age-filter")
            if path == "/api/v3/devicestatus":
                for field in ("date", "created_at"):
                    if f"{field}$gte" in query and f"{field}$lte" in query:
                        observed.add(f"device-status-{field}")
        else:
            if path == "/api/v1/treatments.json" and "find[eventType][$in][0]" in query:
                observed.add("age-filter")
            if path == "/api/v1/devicestatus" and "find[created_at][$gte]" in query and "find[created_at][$lte]" in query:
                observed.add("device-status-created_at")

    trace.on_request_end.append(collect)
    return trace, observed


async def check_ages(client, seed, anchor):
    expected = {label: (anchor - timedelta(hours=hours), label) for label, hours in seed["age_hours"].items()}
    markers = await client.async_get_age_markers(tuple(expected), anchor)
    require({key: (marker.time, marker.event_type) for key, marker in markers.items()} == expected,
            "native maintenance exact labels and occurrence times")
    require(await client.async_get_age_markers(("Synthetic Missing Signal",), anchor) == {},
            "successful empty maintenance lookup")


async def check_reservoir(client, seed, anchor):
    require(await client.async_get_reservoir(anchor, None) is None, "competing uploaders remain unbound")
    for selector in ("primary-zero", "other-fraction"):
        row = next(item for item in seed["device_status"] if item["case"] == selector)
        reading = await client.async_get_reservoir(anchor, row["uploader"])
        contact = anchor - timedelta(minutes=row["contact_minutes_ago"])
        require(reading is not None and (reading.units, reading.source_time, reading.uploader)
                == (row["units"], contact, row["uploader"]), f"native reservoir {selector}")
    require(await client.async_get_reservoir(anchor - timedelta(hours=2), None) is None,
            "successful empty device-status interval")


async def prove_signals(url, access, anchor_iso, *, protected):
    from aiohttp import ClientSession, ClientTimeout
    from custom_components.nightscout_extended.api import NightscoutClient

    anchor = datetime.fromisoformat(anchor_iso)
    seed = json.loads((Path(__file__).with_name("seed.json")).read_text(encoding="utf-8"))["signals"]
    trace, observed = trace_signals(protected)
    async with ClientSession(timeout=ClientTimeout(total=15), trace_configs=[trace]) as session:
        client = NightscoutClient(session, url, access)
        await client.async_get_status()
        outcomes = await asyncio.gather(
            check_ages(client, seed, anchor), check_reservoir(client, seed, anchor), return_exceptions=True,
        )
    required = {
        "ages": {"age-filter"},
        "reservoir": {"device-status-created_at", "device-status-date"} if protected else {"device-status-created_at"},
    }
    verdicts = {}
    for name, outcome in zip(required, outcomes, strict=True):
        if isinstance(outcome, SignalCheckError):
            verdicts[name] = str(outcome)
        elif isinstance(outcome, BaseException):
            verdicts[name] = type(outcome).__name__
        else:
            verdicts[name] = "passed" if required[name] <= observed else "expected native signal routes missing"
    print(json.dumps({"native_signals": {
        "mode": "v3" if protected else "legacy", "age_labels": len(seed["age_hours"]),
        "checks": verdicts, "wire": sorted(observed),
    }}))
    return all(value == "passed" for value in verdicts.values())
