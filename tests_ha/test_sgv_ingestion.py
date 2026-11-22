"""Exercise the ordered Nightscout feed through Home Assistant's public history action."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import re

import pytest
from aiohttp import ClientConnectionError
from homeassistant import config_entries
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.const import (
    CONF_API_KEY,
    CONF_URL,
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import DATA_CUSTOM_COMPONENTS
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_test_home_assistant,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.nightscout_extended.const import DOMAIN

NOW = "2026-01-08T12:11:00+00:00"
WINDOW_START = "2026-01-08T12:00:00+00:00"
WINDOW_END = "2026-01-08T12:10:30+00:00"
LATEST = "2026-01-08T12:10:00.007+00:00"
OLDER = "2026-01-02T08:00:00+00:00"
OLDER_SAMPLE = "2026-01-02T08:05:00+00:00"
RETENTION_START = "2026-01-07T12:11:00+00:00"
EARLY_STAMP = "2026-01-08T11:40:00+00:00"

SUBJECT = "fixture-reader"
BEARER = "fixture-bearer"
ENTRIES = "/api/v3/entries"
LEGACY_ENTRIES = "/api/v1/entries.json"
HISTORY = f"{ENTRIES}/history/"
PROPERTIES = "/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal"


def _ms(value):
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (when - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


def _query_time(value):
    return int(value) if value.isdecimal() else _ms(value)


class _Nightscout:
    """A mutable synthetic site with bounded raw SEARCH and strict HISTORY pages."""

    def __init__(self, mocker: AiohttpClientMocker, *, name="Timeline", v3=True):
        self.name = name
        self.url = f"https://{name.lower()}.example.invalid"
        self.v3 = v3
        self.docs = {}
        self.bgnow = {}
        self.status_settings = {}
        self.calculations = {}
        self.duplicate_sgvs = set()
        self.raw_batches = []
        self.offline = False
        self.fail_search_through = None
        self.fail_history_continuation = False
        self.history_faults = []
        self._first_full_history_cursor = None
        self.requests = []
        self._stamp = 0
        mocker.get(re.compile(rf"^{re.escape(self.url)}/api/"), side_effect=self._answer)

    def put(
        self, identity, date, value, *, direction=None, units=None, modified=None,
        deleted=False, identifier=True,
    ):
        stamp = max(_ms(dt_util.utcnow()), self._stamp + 1) if modified is None else _ms(modified)
        self._stamp = max(self._stamp, stamp)
        row = {
            "_id": f"storage-{identity}" if identifier and self.v3 else identity,
            "type": "sgv",
            "date": _ms(date),
            "sgv": value,
            "srvModified": stamp,
            "isValid": not deleted,
        }
        if identifier and self.v3:
            row["identifier"] = identity
        if direction is not None:
            row["direction"] = direction
        if units is not None:
            row["units"] = units
        self.docs[identity] = row
        return stamp

    def _search_rows(self, url):
        query = url.query
        if self.v3:
            low, high = query.get("date$gte"), query.get("date$lt")
            descending = query.get("sort$desc") == "date"
            start, count = int(query.get("skip", 0)), int(query.get("limit", 1000))
        else:
            low, high = query.get("find[date][$gte]"), query.get("find[date][$lt]")
            descending = True
            start, count = 0, int(query.get("count", 10))
        assert (query.get("type") if self.v3 else query.get("find[type]")) == "sgv"
        rows = [
            row for row in self.docs.values()
            if row["isValid"]
            and (low is None or row["date"] >= _query_time(low))
            and (high is None or row["date"] < _query_time(high))
        ]
        ordered = sorted(rows, key=lambda row: (row["date"], row["_id"]), reverse=descending)
        return self._duplicate_rows(ordered)[start:start + count]

    def _duplicate_rows(self, rows):
        result = []
        for row in rows:
            result.append(row)
            if row.get("identifier", row["_id"]) in self.duplicate_sgvs:
                result.append(row.copy())
        return result

    def _record_raw(self, path, rows):
        self.raw_batches.append((path, [(row["date"], row["sgv"], row["srvModified"]) for row in rows]))

    async def _answer(self, method, url, _data):
        self.requests.append((url.path, dict(url.query)))
        if self.offline:
            raise ClientConnectionError("synthetic site offline")

        def reply(result, *, v3=False, status=200):
            if v3:
                result = {"status": status, "result": result}
            return AiohttpClientMockResponse(method, url, json=result, status=status)

        if url.path == "/api/v1/status.json":
            settings = {"units": "mg/dl", "customTitle": self.name, **self.status_settings}
            return reply({"version": "15.0.9", "settings": settings})
        if url.path == "/api/v2/properties/bgnow":
            return reply({"bgnow": self.bgnow})
        if url.path == PROPERTIES:
            return reply({"bgnow": self.bgnow, **self.calculations})
        if url.path == "/api/v3/version":
            if not self.v3:
                return reply({}, status=404)
            return reply({"apiVersion": "3.0.5"}, v3=True)
        if url.path == f"/api/v2/authorization/request/{SUBJECT}":
            now = int(dt_util.utcnow().timestamp())
            return reply({"token": BEARER, "iat": now, "exp": now + 86400})
        if url.path == "/api/v3/lastModified":
            now = _ms(dt_util.utcnow())
            return reply({"srvDate": now, "collections": {"entries": self._stamp or now}}, v3=True)
        if url.path.startswith(HISTORY):
            cursor = int(url.path.removeprefix(HISTORY))
            if (
                self.fail_history_continuation
                and self._first_full_history_cursor is not None
                and cursor > self._first_full_history_cursor
            ):
                self.history_faults.append(url.path)
                return reply({}, status=503)
            limit = int(url.query.get("limit", 1000))
            rows = (row for row in self.docs.values() if row["srvModified"] > cursor)
            ordered = sorted(rows, key=lambda row: row["srvModified"])
            page = self._duplicate_rows(ordered)[:limit]
            if self.fail_history_continuation and len(page) == limit:
                self._first_full_history_cursor = cursor
            self._record_raw(url.path, page)
            return reply(page, v3=True)
        if url.path in {ENTRIES, LEGACY_ENTRIES}:
            if (
                self.fail_search_through is not None
                and _query_time(url.query["date$gte"]) <= self.fail_search_through
            ):
                return reply({}, status=503)
            rows = self._search_rows(url)
            self._record_raw(url.path, rows)
            return reply(rows, v3=self.v3)
        if url.path in {"/api/v1/devicestatus/", "/api/v3/devicestatus"}:
            return reply([], v3=url.path == "/api/v3/devicestatus")
        if url.path in {"/api/v1/treatments.json", "/api/v3/treatments"}:
            return reply([], v3=url.path == "/api/v3/treatments")
        raise AssertionError(f"Unhandled synthetic Nightscout route: {url.path}")


async def _configure(hass, site):
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert form["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_URL: site.url, CONF_API_KEY: SUBJECT},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED
    return entry


def _entity(hass, entry, key="glucose"):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.unique_id}_{key}")
    assert entity_id is not None
    return entity_id


async def _history(hass, entry, start=WINDOW_START, end=WINDOW_END, *, entity_id=None, context=None):
    reply = await hass.services.async_call(
        DOMAIN,
        "get_glucose_history",
        {"entity_id": entity_id or _entity(hass, entry), "start": start, "end": end},
        blocking=True,
        return_response=True,
        context=context,
    )
    assert reply["unit"] == "mg/dL"
    return reply


def _sample(identity, date, value, direction=None):
    return {"id": identity, "date": _ms(date), "sgv": value, "direction": direction}


def _assert_rows(reply, rows, *, status="complete", missing=()):
    assert reply["status"] == status
    assert reply["missing_ranges"] == [{"start": _ms(start), "end": _ms(end)} for start, end in missing]
    assert reply["readings"] == rows


def _current(hass, entry, value, when):
    assert hass.states.get(_entity(hass, entry)).state == value
    actual = hass.states.get(_entity(hass, entry, "last_reading")).state
    if when in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
        assert actual == when
    else:
        # HA's timestamp device class serializes its state to whole seconds.
        assert actual == datetime.fromisoformat(when).isoformat(timespec="seconds")


async def _tick(hass, freezer, site):
    site.requests.clear()
    site.raw_batches.clear()
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert any(path == PROPERTIES for path, _ in site.requests)


async def _unload(hass, *entries):
    for entry in entries:
        if entry.state is config_entries.ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


@asynccontextmanager
async def _runtime(config_dir):
    """Use the HA bootstrap and shutdown used by the existing restart observer."""
    async with async_test_home_assistant(
        config_dir=str(config_dir),
        load_registries=False,
        initial_state=CoreState.not_running,
    ) as hass:
        try:
            hass.data.pop(DATA_CUSTOM_COMPONENTS)
            assert await async_load_base_functionality(hass)
            await hass.async_start()
            await hass.async_block_till_done()
            yield hass
        finally:
            try:
                await _unload(hass, *hass.config_entries.async_entries(DOMAIN))
            finally:
                await hass.async_stop(force=True)


async def test_scheduled_polls_materialize_every_sgv_and_apply_source_mutations(
    hass: HomeAssistant, aioclient_mock, freezer,
):
    freezer.move_to(NOW)
    site = _Nightscout(aioclient_mock)
    entry = await _configure(hass, site)
    try:
        for _ in range(2):
            await _tick(hass, freezer, site)
            _assert_rows(await _history(hass, entry), [])
            _current(hass, entry, STATE_UNKNOWN, STATE_UNKNOWN)

        site.put("latest", LATEST, 124, direction="Flat")
        await _tick(hass, freezer, site)
        latest = _sample("latest", LATEST, 124, "Flat")
        _assert_rows(await _history(hass, entry), [latest])
        _current(hass, entry, "124", LATEST)

        late = "2026-01-08T12:05:00.401+00:00"
        six = "2026-01-08T12:06:00.003+00:00"
        eight = "2026-01-08T12:08:00.501+00:00"
        # This is server-write order. Measurement order differs, including a tie.
        site.put("tie-b", eight, 121, identifier=False)
        site.put("late-05", late, 6.5, units="mmol/L")
        site.put("tie-a", eight, 119)
        site.put("six", six, 116)
        await _tick(hass, freezer, site)
        samples = [
            _sample("late-05", late, pytest.approx(117.1183)),
            _sample("six", six, 116),
            _sample("tie-a", eight, 119),
            _sample("tie-b", eight, 121),
            latest,
        ]
        _assert_rows(await _history(hass, entry), samples)
        _current(hass, entry, "124", LATEST)

        # An overlap replay cannot turn an old measurement into a new one.
        for _ in range(2):
            await _tick(hass, freezer, site)
            _assert_rows(await _history(hass, entry), samples)
            _current(hass, entry, "124", LATEST)

        corrected = "2026-01-08T12:07:00.625+00:00"
        site.put("tie-a", corrected, 123)
        site.put("tie-b", eight, 121, deleted=True, identifier=False)
        await _tick(hass, freezer, site)
        _assert_rows(await _history(hass, entry), [*samples[:2], _sample("tie-a", corrected, 123), latest])
        _current(hass, entry, "124", LATEST)
        _assert_rows(await _history(hass, entry, end=LATEST), [*samples[:2], _sample("tie-a", corrected, 123)])
    finally:
        await _unload(hass, entry)


@pytest.mark.parametrize("v3", [False, True], ids=["legacy", "v3"])
async def test_scheduled_timeline_preserves_source_times_and_public_history(
    hass: HomeAssistant, aioclient_mock, freezer, v3,
):
    freezer.move_to(NOW)
    site = _Nightscout(aioclient_mock, v3=v3)
    first = "2026-01-08T12:10:00+00:00"
    same_value_later = "2026-01-08T12:13:30+00:00"
    late = "2026-01-08T12:09:20+00:00"
    other_late = "2026-01-08T12:12:00+00:00"
    recovered = "2026-01-08T12:16:30+00:00"
    site.put("first", first, 124, direction="Flat", modified=EARLY_STAMP)
    site.bgnow = {"last": 124, "mills": _ms(first)}
    entry = await _configure(hass, site)

    async def assert_timeline(readings):
        # Keep the requested end inside the latest successful poll's coverage.
        end = datetime.fromisoformat(readings[-1][0]) + timedelta(milliseconds=1)
        requests = len(site.requests)
        result = await _history(hass, entry, end=end.isoformat())
        assert (result["status"], result["missing_ranges"]) == ("complete", [])
        assert [(row["date"], row["sgv"]) for row in result["readings"]] == [
            (_ms(when), value) for when, value in readings
        ]
        assert len(site.requests) == requests

    def raw_rows():
        return [
            row for path, rows in site.raw_batches
            if (path.startswith(HISTORY) if v3 else path == LEGACY_ENTRIES)
            for row in rows
        ]

    try:
        expected = [(first, 124)]
        _current(hass, entry, "124", first)
        await assert_timeline(expected)

        # No writes since bootstrap. An empty versioned change feed and an
        # unchanged legacy snapshot both leave the same source sample current.
        await _tick(hass, freezer, site)
        assert entry.runtime_data.last_update_success
        assert raw_rows() == ([] if v3 else [(_ms(first), 124, _ms(EARLY_STAMP))])
        _current(hass, entry, "124", first)
        await assert_timeline(expected)

        # A storage-side rewrite of the same measurement, then an exact raw
        # replay, are distinct from a new measurement.
        rewritten = site.put("first", first, 124, direction="Flat")
        await _tick(hass, freezer, site)
        first_batch = raw_rows()
        assert first_batch == [(_ms(first), 124, rewritten)]
        _current(hass, entry, "124", first)
        await assert_timeline(expected)
        await _tick(hass, freezer, site)
        assert raw_rows() == first_batch
        assert entry.runtime_data.last_update_success
        _current(hass, entry, "124", first)
        await assert_timeline(expected)

        site.put("same-value-later", same_value_later, 124, direction="Flat")
        site.bgnow = {"last": 124, "mills": _ms(same_value_later)}
        await _tick(hass, freezer, site)
        expected = [*expected, (same_value_later, 124)]
        _current(hass, entry, "124", same_value_later)
        await assert_timeline(expected)

        # Upload the later-measured backfill first. The raw feed also repeats
        # an exact document within this batch.
        site.put("other-late", other_late, 137)
        late_write = site.put("late", late, 111)
        site.duplicate_sgvs.add("late")
        await _tick(hass, freezer, site)
        assert raw_rows().count((_ms(late), 111, late_write)) == 2
        expected = [(late, 111), expected[0], (other_late, 137), expected[1]]
        _current(hass, entry, "124", same_value_later)
        await assert_timeline(expected)

        site.offline = True
        await _tick(hass, freezer, site)
        assert not entry.runtime_data.last_update_success
        assert entry.state is config_entries.ConfigEntryState.LOADED
        _current(hass, entry, STATE_UNAVAILABLE, STATE_UNAVAILABLE)
        await assert_timeline(expected)

        site.offline = False
        site.duplicate_sgvs.clear()
        site.put("recovered", recovered, 131)
        site.bgnow = {"last": 131, "mills": _ms(recovered)}
        await _tick(hass, freezer, site)
        assert entry.runtime_data.last_update_success
        _current(hass, entry, "131", recovered)
        await assert_timeline([*expected, (recovered, 131)])
    finally:
        await _unload(hass, entry)


async def test_failed_second_history_page_keeps_committed_rows_and_retries_the_backlog(
    hass: HomeAssistant, aioclient_mock, freezer,
):
    freezer.move_to(NOW)
    site = _Nightscout(aioclient_mock)
    site.put("committed", LATEST, 124, modified=EARLY_STAMP)
    entry = await _configure(hass, site)
    try:
        original = [_sample("committed", LATEST, 124)]
        committed = await _history(hass, entry)
        _assert_rows(committed, original)
        # Exceed the pinned server's default maximum page size (1000).
        backlog_start = datetime(2026, 1, 8, 12, 1, tzinfo=UTC)
        first_stamp = dt_util.utcnow()
        expected = []
        for index in range(1001):
            date = backlog_start + timedelta(milliseconds=index * 137)
            identity = f"backlog-{index:04}"
            # Timestamp cohorts straddle an even-sized page boundary.
            modified = first_stamp + timedelta(milliseconds=(index + 1) // 2)
            site.put(identity, date, 100 + index % 17, modified=modified)
            expected.append(_sample(identity, date, 100 + index % 17))

        # Allow the preceding tail SEARCH. Fail only the HISTORY request
        # that continues an observed full page.
        site.fail_history_continuation = True
        await _tick(hass, freezer, site)
        failed_attempt = [path for path, _ in site.requests if path.startswith(HISTORY)]
        assert len(set(failed_attempt)) >= 2
        assert site.history_faults and site.history_faults[0] == failed_attempt[1]
        assert await _history(hass, entry) == committed

        site.fail_history_continuation = False
        await _tick(hass, freezer, site)
        retry = [path for path, _ in site.requests if path.startswith(HISTORY)]
        assert retry[:2] == failed_attempt[:2]
        _assert_rows(await _history(hass, entry), [*expected, *original])
        await _tick(hass, freezer, site)
        _assert_rows(await _history(hass, entry), [*expected, *original])
        _current(hass, entry, "124", LATEST)
    finally:
        await _unload(hass, entry)


@pytest.mark.parametrize("v3", [False, True], ids=["legacy", "v3"])
async def test_retained_history_works_offline_and_older_ranges_are_fetched_on_demand(
    hass: HomeAssistant, aioclient_mock, freezer, v3,
):
    freezer.move_to(NOW)
    site = _Nightscout(aioclient_mock, v3=v3)
    site.put("older", OLDER_SAMPLE, 107, modified=EARLY_STAMP)
    site.put("retained", LATEST, 124, modified=EARLY_STAMP)
    site.bgnow = {"last": 124, "mills": _ms(LATEST)}
    entry = await _configure(hass, site)
    try:
        retained = _sample("retained", LATEST, 124)
        older = _sample("older", OLDER_SAMPLE, 107)
        committed = await _history(hass, entry)
        _assert_rows(committed, [retained])
        as_of = datetime.fromisoformat(committed["as_of"])
        assert as_of.utcoffset() == timedelta(0)

        site.offline = True
        site.requests.clear()
        _assert_rows(await _history(hass, entry), [retained])
        assert site.requests == []
        partial = await _history(hass, entry, start=OLDER)
        _assert_rows(partial, [retained], status="partial", missing=[(OLDER, RETENTION_START)])
        assert partial["as_of"] == committed["as_of"]
        _assert_rows(
            await _history(hass, entry, OLDER, RETENTION_START), [],
            status="unavailable", missing=[(OLDER, RETENTION_START)],
        )

        site.offline = False
        site.requests.clear()
        _assert_rows(await _history(hass, entry, start=OLDER), [older, retained])
        search_path = ENTRIES if v3 else LEGACY_ENTRIES
        keys = ("date$gte", "date$lt") if v3 else ("find[date][$gte]", "find[date][$lt]")
        bounds = {
            tuple(_query_time(query[key]) for key in keys)
            for path, query in site.requests
            if path == search_path
        }
        assert bounds == {(_ms(OLDER), _ms(RETENTION_START))}
        _assert_rows(await _history(hass, entry, OLDER, OLDER_SAMPLE), [])

        site.offline = True
        site.requests.clear()
        _assert_rows(await _history(hass, entry, start=OLDER), [older, retained])
        assert site.requests == []

        # Extend the request into an uncached older range without advancing the
        # clock or triggering another poll. The cached and durable rows remain usable.
        earlier = "2026-01-01T08:00:00+00:00"
        partial = await _history(hass, entry, start=earlier)
        _assert_rows(partial, [older, retained], status="partial", missing=[(earlier, OLDER)])
        assert partial["as_of"] == committed["as_of"]
        assert {path for path, _ in site.requests} == {search_path}
        assert {tuple(_query_time(query[key]) for key in keys) for _, query in site.requests} == {
            (_ms(earlier), _ms(OLDER))
        }
    finally:
        await _unload(hass, entry)


async def test_reload_and_home_assistant_restart_restore_committed_rows(
    aioclient_mock, freezer, hass_storage, tmp_path,
):
    freezer.move_to(NOW)
    site = _Nightscout(aioclient_mock)
    site.put("persisted", LATEST, 124, modified=EARLY_STAMP)
    expected = [_sample("persisted", LATEST, 124)]
    async with _runtime(tmp_path) as first:
        entry = await _configure(first, site)
        entry_id = entry.entry_id
        entity_id = _entity(first, entry)
        _assert_rows(await _history(first, entry), expected)
        _current(first, entry, "124", LATEST)

        # Deny SEARCH that could reacquire the old sample while allowing
        # SEARCH of the newly uncovered tail and quiet HISTORY.
        site.fail_search_through = _ms(LATEST)
        assert await first.config_entries.async_reload(entry_id)
        await first.async_block_till_done()
        _assert_rows(await _history(first, entry), expected)
        _current(first, entry, "124", LATEST)

    async with _runtime(tmp_path) as restarted:
        entry = restarted.config_entries.async_get_entry(entry_id)
        assert entry is not None
        assert await restarted.config_entries.async_setup(entry_id)
        await restarted.async_block_till_done()
        assert entry.state is config_entries.ConfigEntryState.LOADED
        assert _entity(restarted, entry) == entity_id
        _assert_rows(await _history(restarted, entry), expected)
        _current(restarted, entry, "124", LATEST)
        await _tick(restarted, freezer, site)
        _assert_rows(await _history(restarted, entry), expected)
        _current(restarted, entry, "124", LATEST)


async def test_history_requires_read_access_to_the_requested_site_glucose_entity(
    hass: HomeAssistant, aioclient_mock, freezer, hass_read_only_user,
):
    freezer.move_to(NOW)
    first_site = _Nightscout(aioclient_mock, name="First")
    second_site = _Nightscout(aioclient_mock, name="Second")
    first_site.put("source-id", LATEST, 124, modified=EARLY_STAMP)
    second_site.put("source-id", LATEST, 178, modified=EARLY_STAMP)
    first_entry = await _configure(hass, first_site)
    second_entry = await _configure(hass, second_site)
    try:
        allowed_entity = _entity(hass, first_entry)
        hass_read_only_user.mock_policy({"entities": {"entity_ids": {allowed_entity: True}}})
        context = Context(user_id=hass_read_only_user.id)
        own = await _history(hass, first_entry, context=context)
        _assert_rows(own, [_sample("source-id", LATEST, 124)])
        _assert_rows(await _history(hass, second_entry), [_sample("source-id", LATEST, 178)])

        for site in (first_site, second_site):
            site.requests.clear()
        with pytest.raises(Unauthorized):
            await _history(hass, second_entry, start=OLDER, context=context)
        with pytest.raises(ServiceValidationError):
            await _history(hass, first_entry, start=OLDER, entity_id=_entity(hass, first_entry, "last_reading"))
        assert first_site.requests == second_site.requests == []
    finally:
        await _unload(hass, first_entry, second_entry)
