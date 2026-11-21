"""Assert the real Nightscout calendar lookback and candidate window."""

from datetime import datetime, timedelta
import json


DEFAULT_LENGTH = timedelta(minutes=5)


def trace_calendar(protected):
    from aiohttp import TraceConfig

    observed = set()
    trace = TraceConfig()

    async def collect(_session, _context, params):
        path, query = params.url.path, params.url.query
        if params.method != "GET" or params.response.status != 200:
            return
        if protected and path == "/api/v3/treatments":
            if (query.get("duration$gt"), query.get("sort$desc"), query.get("limit"), query.get("skip")) == (
                "5", "duration", "1", "0",
            ):
                observed.add("v3-descending-duration")
        if not protected and path == "/api/v1/treatments.json":
            if (query.get("find[duration][$gt]"), query.get("sort[duration]"), query.get("count")) == (
                "5", "desc", "1",
            ) and "find[created_at][$lt]" in query:
                observed.add("v1-descending-duration")

    trace.on_request_end.append(collect)
    return trace, observed


async def check_calendar(client, observed, fixture, *, protected):
    start, end = (datetime.fromisoformat(value) for value in fixture["window"])
    before = set(observed)
    records = await client.async_get_calendar_treatments(start, end, DEFAULT_LENGTH)
    overlapping = [
        item for item in records
        if item.time < end and item.time + timedelta(minutes=item.duration_min or 5) > start
    ]
    expected = fixture["overlap"]
    actual = [(item.uid, round(item.time.timestamp() * 1000), item.duration_min) for item in overlapping]
    correct = actual == [(expected["id"], expected["millis"], expected["duration"])]
    probe = "v3-descending-duration" if protected else "v1-descending-duration"
    wire = sorted(observed - before)
    print(json.dumps({"calendar": {"mode": "v3" if protected else "legacy", "match": correct, "wire": wire}}))
    return correct and probe in wire
