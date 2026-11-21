"""Exercise ordered SGV commits against a synthetic Nightscout source."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from homeassistant.util.file import WriteError

from custom_components.nightscout_extended import sgv_history
from custom_components.nightscout_extended.api import NightscoutConnectionError
from custom_components.nightscout_extended.sgv_history import SGVHistory
from custom_components.nightscout_extended.sgv_source import SGVReconciliationRequired

NOW = datetime(2025, 1, 19, 12, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
DAY = timedelta(days=1)


def row(identity, when, value, modified=None):
    return {
        "id": identity,
        "date": int(when.timestamp() * 1000),
        "sgv": value,
        "direction": "Flat",
        "modified": modified,
    }


class Source:
    """Supply only requested half-open windows and scripted deltas."""

    uses_v3 = True
    site_id = "test-site"

    def __init__(self, *rows):
        self.rows = list(rows)
        self.ranges = []
        self.cursors = []
        self.fences = 0
        self.fence = NOW_MS
        self.changes = []
        self.unavailable = False

    async def read_range(self, start, end):
        self.ranges.append((start, end))
        if self.unavailable:
            raise NightscoutConnectionError("synthetic source unavailable")
        return [reading for reading in self.rows if start <= reading["date"] < end]

    async def change_fence(self):
        self.fences += 1
        return self.fence

    async def read_changes(self, after):
        self.cursors.append(after)
        response = self.changes.pop(0) if self.changes else ([], None)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def entry():
    """Give each test its own actual Home Assistant storage file."""
    return uuid4().hex


async def test_batch_order_edits_and_quiet_refresh(hass, entry):
    source = Source()
    history = SGVHistory(hass, entry, source, 1)
    original = NOW - timedelta(minutes=1)
    older = NOW - timedelta(minutes=5)
    same_minute = timedelta(seconds=12)
    readings = [
        row("d", original, 141, NOW_MS + 4),
        row("b", older, 129, NOW_MS + 2),
        row("c", older + same_minute, 135, NOW_MS + 3),
        row("a", older, 128, NOW_MS + 1),
    ]
    source.changes.append((readings, NOW_MS + 4))
    await history.async_refresh(NOW)

    async def raw():
        response = await history.async_range(NOW - DAY, NOW)
        assert response["status"] == "complete"
        return [(r["id"], r["date"], r["sgv"]) for r in response["readings"]]

    expected = [
        ("a", readings[3]["date"], 128),
        ("b", readings[1]["date"], 129),
        ("c", readings[2]["date"], 135),
        ("d", readings[0]["date"], 141),
    ]
    assert await raw() == expected
    assert history.select_current(NOW, None, None) == (original, 141, "Flat")
    revision = history.revision
    for seconds in (20, 40):
        await history.async_refresh(NOW + timedelta(seconds=seconds))
        assert history.revision == revision
        assert history.select_current(NOW, None, None) == (original, 141, "Flat")
    # Read the retained window after its lower bound has slid forward.
    response = await history.async_range(NOW - DAY + timedelta(minutes=1), NOW)
    assert response["status"] == "complete"
    assert [(r["id"], r["date"], r["sgv"]) for r in response["readings"]] == expected

    backfill = row("late", NOW - timedelta(hours=1), 110, NOW_MS + 5)
    source.changes.append(([backfill], NOW_MS + 5))
    await history.async_refresh(NOW + timedelta(seconds=50))
    assert history.select_current(NOW, None, None) == (original, 141, "Flat")
    assert history.revision > revision

    moved = row("d", older - timedelta(minutes=1), 143, NOW_MS + 6)
    removed = {"id": "b", "deleted": True, "modified": NOW_MS + 7}
    source.changes.append(([moved, removed], NOW_MS + 7))
    await history.async_refresh(NOW + timedelta(minutes=1))
    result = await history.async_range(NOW - timedelta(hours=3), NOW)
    assert [r["id"] for r in result["readings"]] == ["late", "d", "a", "c"]
    assert history.select_current(NOW, None, None) == (older + same_minute, 135, "Flat")


async def test_current_uses_exact_clock_and_corroboration(hass, entry):
    past = NOW - timedelta(minutes=1)
    future = NOW + timedelta(milliseconds=59_999)
    source = Source(
        row("past", past, 143),
        row("sensor-error", NOW, 38),
        row("future", future, 144),
        row("at-limit", NOW + timedelta(seconds=60), 145),
    )
    history = SGVHistory(hass, entry, source, 1)
    await history.async_refresh(NOW)
    assert history.select_current(NOW, None, None) == (past, 143, "Flat")
    assert history.select_current(NOW, future, 143) == (past, 143, "Flat")
    assert history.select_current(NOW, future, 144) == (future, 144, "Flat")
    assert history.select_current(NOW, NOW + timedelta(seconds=60), 145) == (past, 143, "Flat")
    assert history.select_current(future, None, None) == (future, 144, "Flat")


async def test_unusable_edit_removes_previous_measurement(hass, entry):
    source = Source(row("edited", NOW - timedelta(minutes=1), 142, NOW_MS - 1))
    history = SGVHistory(hass, entry, source, 1)
    await history.async_refresh(NOW)
    assert history.select_current(NOW, None, None)[1] == 142
    source.changes.append(([{"id": "edited", "modified": NOW_MS + 1, "invalid": True}], NOW_MS + 1))
    await history.async_refresh(NOW + timedelta(seconds=1))
    result = await history.async_range(NOW - timedelta(hours=1), NOW)
    assert result["status"] == "complete"
    assert result["readings"] == []
    assert history.select_current(NOW, None, None) is None


async def test_network_and_native_write_failures_keep_retry_cursor(hass, entry, monkeypatch):
    original = row("current", NOW - timedelta(minutes=2), 140, NOW_MS + 1)
    changed = row("current", NOW - timedelta(minutes=3), 135, NOW_MS + 2)
    source = Source()
    history = SGVHistory(hass, entry, source, 1)
    source.changes = [
        ([original], NOW_MS + 1),
        NightscoutConnectionError("later page failed"),
        ([changed], NOW_MS + 2),
        ([changed], NOW_MS + 2),
    ]
    await history.async_refresh(NOW)
    with pytest.raises(NightscoutConnectionError):
        await history.async_refresh(NOW + timedelta(seconds=1))

    def fail(*args, **kwargs):
        raise WriteError("synthetic write failure")

    with monkeypatch.context() as patch:
        patch.setattr(sgv_history, "write_utf8_file_atomic", fail)
        with pytest.raises(NightscoutConnectionError):
            await history.async_refresh(NOW + timedelta(seconds=2))
    reopened = SGVHistory(hass, entry, source, 1)
    await reopened.async_load()
    assert reopened.select_current(NOW, None, None)[1] == 140

    await history.async_refresh(NOW + timedelta(seconds=3))
    assert history.select_current(NOW, None, None)[1] == 135
    assert source.cursors == [NOW_MS, NOW_MS + 1, NOW_MS + 1, NOW_MS + 1]
    await reopened.async_load()
    assert reopened.select_current(NOW, None, None)[1] == 135
    assert reopened.revision == history.revision


async def test_saturation_rebuilds_authoritative_window(hass, entry):
    source = Source(row("hard-deleted", NOW - timedelta(minutes=2), 142))
    history = SGVHistory(hass, entry, source, 1)
    await history.async_refresh(NOW)
    source.rows = [row("unstamped", NOW - timedelta(minutes=3), 137)]
    source.fence = NOW_MS + 30_000
    source.changes.append(SGVReconciliationRequired("unpageable change stamp"))
    await history.async_refresh(NOW + timedelta(seconds=30))
    result = await history.async_range(NOW - timedelta(hours=1), NOW)
    assert [r["id"] for r in result["readings"]] == ["unstamped"]
    assert source.fences == 2
    assert source.cursors == [NOW_MS, NOW_MS, NOW_MS + 30_000]


async def test_ranges_cover_only_completed_intervals_and_do_not_expand_retention(hass, entry):
    start = NOW - DAY - timedelta(hours=1)
    recent = row("retained", NOW - timedelta(minutes=5), 144)
    old = row("older", start, 133)
    source = Source(old, recent)
    source.uses_v3 = False
    history = SGVHistory(hass, entry, source, 1)
    await history.async_refresh(NOW)
    assert source.fences == 0

    empty = await history.async_range(NOW - timedelta(hours=2), NOW - timedelta(hours=1))
    assert empty["status"] == "complete"
    assert empty["readings"] == []
    assert empty["missing_ranges"] == []
    source.unavailable = True
    reply = await history.async_range(start, NOW)
    assert reply["status"] == "partial"
    assert reply["as_of"] == NOW.isoformat()
    assert [r["id"] for r in reply["readings"]] == ["retained"]
    expected_gap = (old["date"], NOW_MS - 86_400_000)
    assert reply["missing_ranges"] == [{"start": expected_gap[0], "end": expected_gap[1]}]

    source.unavailable = False
    fetched = await history.async_range(start, NOW)
    assert source.ranges[-1] == expected_gap
    assert fetched["status"] == "complete"
    assert [r["id"] for r in fetched["readings"]] == ["older", "retained"]
    calls = len(source.ranges)
    assert await history.async_range(start, NOW) == fetched
    assert len(source.ranges) == calls

    reopened = SGVHistory(hass, entry, source, 1)
    source.unavailable = True
    offline = await reopened.async_range(start, NOW, allow_remote=False)
    assert offline == reply
    assert len(source.ranges) == calls
    unknown = await reopened.async_range(start, NOW - DAY, allow_remote=False)
    assert unknown["status"] == "unavailable"
    assert unknown["missing_ranges"] == reply["missing_ranges"]


async def test_legacy_quiet_polls_preserve_exact_committed_reading(hass, entry):
    measured = NOW - timedelta(minutes=2, milliseconds=375)
    original = row("legacy-recent", measured, 144)
    source = Source(original)
    source.uses_v3 = False
    history = SGVHistory(hass, entry, source, 1)
    await history.async_refresh(NOW)
    revision = history.revision

    source.rows.clear()
    for seconds in (15, 30):
        await history.async_refresh(NOW + timedelta(seconds=seconds))
        assert history.revision == revision
        assert history.select_current(NOW, None, None) == (measured, 144, "Flat")

    reopened = SGVHistory(hass, entry, source, 1)
    result = await reopened.async_range(NOW - timedelta(hours=1), NOW, allow_remote=False)
    assert result["status"] == "complete"
    assert result["readings"] == [
        {"id": "legacy-recent", "date": original["date"], "sgv": 144, "direction": "Flat"}
    ]
    assert reopened.revision == revision


async def test_legacy_late_only_poll_merges_and_expires_by_measurement_time(hass, entry):
    recent_time = NOW - timedelta(minutes=3)
    recent = row("legacy-current", recent_time, 142)
    expiring = row("expiring", NOW - DAY + timedelta(seconds=1), 123)
    late = row("legacy-backfill", NOW - timedelta(hours=2), 137)
    source = Source(recent, expiring)
    source.uses_v3 = False
    history = SGVHistory(hass, entry, source, 1)
    await history.async_refresh(NOW)

    source.rows = [late]
    later = NOW + timedelta(seconds=2)
    await history.async_refresh(later)
    result = await history.async_range(later - DAY, later, allow_remote=False)
    assert result["status"] == "complete"
    assert [(r["id"], r["date"], r["sgv"]) for r in result["readings"]] == [
        ("legacy-backfill", late["date"], 137),
        ("legacy-current", recent["date"], 142),
    ]
    assert history.select_current(later, None, None) == (recent_time, 142, "Flat")


async def test_snapshot_is_scoped_and_removal_fences_preexisting_readers(hass, entry):
    original = Source(row("private-to-first-site", NOW, 144))
    history = SGVHistory(hass, entry, original, 1)
    await history.async_refresh(NOW)
    alternate = Source()
    alternate.site_id = "other-test-site"
    reopened = SGVHistory(hass, entry, alternate, 1)
    other_entry = SGVHistory(hass, f"{entry}-other", original, 1)
    for store in (reopened, other_entry):
        await store.async_load()
        assert store.revision is None
        assert store.select_current(NOW, None, None) is None
    await reopened.async_remove()
    assert (await history.async_range(NOW - DAY, NOW, allow_remote=False))["status"] == "unavailable"
    with pytest.raises(NightscoutConnectionError):
        await history.async_refresh(NOW)
