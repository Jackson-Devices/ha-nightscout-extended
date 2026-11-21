"""Ordered SGV snapshots and range reads owned by one HA config entry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from time import monotonic
from typing import Any
from weakref import WeakValueDictionary

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError, write_utf8_file_atomic

from .api import NightscoutAuthError, NightscoutConnectionError
from .const import DOMAIN
from .sgv_source import NightscoutSGVSource, SGVReconciliationRequired

DEFAULT_HISTORY_DAYS = 1
MIN_HISTORY_DAYS = 1
MAX_HISTORY_DAYS = 30

_DAY_MS = 86_400_000
_CLOCK_SKEW_MS = 60_000
_CLOCK_SKEW = timedelta(milliseconds=_CLOCK_SKEW_MS)
_RECONCILE_MS = 15 * 60 * 1000
_ROW_LIMIT = 50_000
_FILE_LIMIT = 24 * 1024 * 1024
_OVERFLOW_SECONDS = 300
_OVERFLOW_LIMIT = 4
_FORMAT_VERSION = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_GATES_KEY = f"{DOMAIN}.sgv_history_gates"
_SOURCE_ERRORS = (NightscoutAuthError, NightscoutConnectionError)


def _ceil_ms(when: datetime) -> int:
    if when.utcoffset() is None:
        raise ValueError("SGV history times must include a timezone")
    micros = (when - _EPOCH) // timedelta(microseconds=1)
    return -(-micros // 1000)


def _from_ms(value: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=value)


@dataclass(frozen=True, slots=True)
class _Reading:
    id: str
    date: int
    sgv: float
    direction: str | None
    modified: int | None

    @classmethod
    def parse(cls, value: dict[str, Any]) -> _Reading:
        identity = value["id"]
        date = value["date"]
        glucose = value["sgv"]
        direction = value.get("direction")
        modified = value.get("modified")
        if (
            not isinstance(identity, str)
            or not identity
            or type(date) is not int
            or type(glucose) not in (int, float)
            or not math.isfinite(glucose)
            or (direction is not None and not isinstance(direction, str))
            or (modified is not None and type(modified) is not int)
        ):
            raise ValueError("Invalid SGV reading")
        return cls(identity, date, float(glucose), direction, modified)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "date": self.date, "sgv": self.sgv, "direction": self.direction}


def _ordered(rows: dict[str, _Reading]) -> tuple[_Reading, ...]:
    return tuple(sorted(rows.values(), key=lambda row: (row.date, row.id)))


@dataclass(frozen=True, slots=True)
class _Snapshot:
    uses_v3: bool
    cursor: int | None
    start: int
    end: int
    checked: int
    reconciled: int
    rows: tuple[_Reading, ...]
    revision: int = 0


@dataclass(frozen=True, slots=True)
class _Overflow:
    start: int
    end: int
    checked: int
    expires: float
    rows: tuple[_Reading, ...]


class _EntryGate:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.generation = 0
        self.removed = False


def _load_snapshot(path: Path, site: str) -> _Snapshot | None:
    try:
        with path.open("rb") as file:
            content = file.read(_FILE_LIMIT + 1)
    except FileNotFoundError:
        return None

    if len(content) > _FILE_LIMIT:
        raise ValueError("SGV storage size exceeds its limit")
    data = json.loads(content)
    if not isinstance(data, dict) or data.get("version") != _FORMAT_VERSION:
        raise ValueError("Unsupported SGV storage version")
    if data["site"] != site:
        return None

    uses_v3 = data["uses_v3"]
    cursor = data["cursor"]
    start, end = data["start"], data["end"]
    checked, reconciled = data["checked"], data["reconciled"]
    revision = data["revision"]
    if (
        type(uses_v3) is not bool
        or (cursor is not None and (type(cursor) is not int or not uses_v3))
        or any(type(mark) is not int for mark in (start, end, checked, reconciled))
        or not start <= checked < end
        or reconciled > checked
        or type(revision) is not int
        or revision < 1
        or not isinstance(data["rows"], list)
        or len(data["rows"]) > _ROW_LIMIT
    ):
        raise ValueError("Invalid SGV storage metadata")
    _from_ms(start)
    _from_ms(end - 1)
    rows = {}
    for value in data["rows"]:
        reading = _Reading.parse(value)
        if reading.id in rows or not start <= reading.date < end:
            raise ValueError("Invalid or repeated stored SGV")
        rows[reading.id] = reading
    return _Snapshot(uses_v3, cursor, start, end, checked, reconciled, _ordered(rows), revision)


def _write_snapshot(path: Path, site: str, state: _Snapshot) -> None:
    data = {
        "version": _FORMAT_VERSION,
        "site": site,
        "uses_v3": state.uses_v3,
        "cursor": state.cursor,
        "start": state.start,
        "end": state.end,
        "checked": state.checked,
        "reconciled": state.reconciled,
        "revision": state.revision,
        "rows": [{**row.public(), "modified": row.modified} for row in state.rows],
    }
    content = json.dumps(data, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(content) > _FILE_LIMIT:
        raise ValueError("SGV storage size exceeds its limit")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    write_utf8_file_atomic(str(path), content, private=True, mode="wb")


def _removes_reading(change: dict[str, Any]) -> bool:
    return change.get("deleted") is True or change.get("invalid") is True


def _apply_changes(
    rows: dict[str, _Reading],
    changes: list[dict[str, Any]],
    start: int,
    end: int,
) -> None:
    """Merge current versions by identity, including moves across interval edges."""
    for change in changes:
        identity = change["id"]
        modified = change["modified"]
        existing = rows.get(identity)
        if existing is not None and existing.modified is not None and existing.modified > modified:
            continue
        if _removes_reading(change):
            rows.pop(identity, None)
            continue
        reading = _Reading.parse(change)
        if start <= reading.date < end:
            rows[identity] = reading
        else:
            rows.pop(identity, None)


def _missing(
    start: int, end: int, covered: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    gaps = []
    for lower, upper in sorted(covered):
        if upper <= start:
            continue
        if lower >= end:
            break
        if lower > start:
            gaps.append((start, lower))
        start = max(start, upper)
        if start >= end:
            return gaps
    if start < end:
        gaps.append((start, end))
    return gaps


class SGVHistory:
    """Synchronize and read one site's bounded raw SGVs."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        source: NightscoutSGVSource,
        days: int = DEFAULT_HISTORY_DAYS,
    ) -> None:
        if type(days) is not int or not MIN_HISTORY_DAYS <= days <= MAX_HISTORY_DAYS:
            raise ValueError("Invalid SGV history retention")
        self._hass = hass
        self._source = source
        self._site = source.site_id
        self._window = days * _DAY_MS
        self._path = Path(hass.config.path(".storage", f"{DOMAIN}.sgv.{entry_id}"))
        gates: WeakValueDictionary[str, _EntryGate] = hass.data.setdefault(
            _GATES_KEY, WeakValueDictionary()
        )
        self._gate = gates.setdefault(entry_id, _EntryGate())
        self._generation = -1
        self._state: _Snapshot | None = None
        self._overflow: list[_Overflow] = []

    @property
    def revision(self) -> int | None:
        """Expose a content token without counting quiet polls as reading changes."""
        if self._gate.removed or self._state is None:
            return None
        return self._state.revision

    def _local_start(self, state: _Snapshot) -> int:
        return max(state.start, state.checked - self._window)

    def _publish(self, state: _Snapshot | None) -> None:
        self._state = state
        self._generation = self._gate.generation

    def _revision_for(self, candidate: _Snapshot) -> int:
        state = self._state
        if state is None:
            return 1
        if (
            state.uses_v3 != candidate.uses_v3
            or state.checked - state.start != self._window
            or len(state.rows) != len(candidate.rows)
            or any(old.public() != new.public() for old, new in zip(state.rows, candidate.rows))
        ):
            return state.revision + 1
        return state.revision

    async def _load_locked(self) -> None:
        if self._generation == self._gate.generation or self._gate.removed:
            return
        try:
            state = await self._hass.async_add_executor_job(_load_snapshot, self._path, self._site)
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as error:
            raise NightscoutConnectionError("Could not load SGV history storage") from error
        self._overflow.clear()
        self._publish(state)

    async def async_load(self) -> None:
        """Load the latest acknowledged or recovered local snapshot."""
        async with self._gate.lock:
            await self._load_locked()

    async def _finish_executor(self, action, *args) -> bool:
        """Finish a mutation before the lock is released, even on cancellation."""
        job = self._hass.async_add_executor_job(action, *args)
        cancelled = False
        while not job.done():
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                if job.cancelled():
                    raise
                cancelled = True
        job.result()
        return cancelled

    async def _commit(self, state: _Snapshot) -> bool:
        if state == self._state:
            return False
        try:
            cancelled = await self._finish_executor(_write_snapshot, self._path, self._site, state)
        except (WriteError, ValueError, OSError) as error:
            # The atomic helper can fail after replacement. Read the actual
            # committed generation before any subsequent read or retry.
            self._gate.generation += 1
            raise NightscoutConnectionError("Could not commit SGV history storage") from error
        self._gate.generation += 1
        self._publish(state)
        return cancelled

    async def _read_window(self, start: int, end: int) -> dict[str, _Reading]:
        report = await self._source.read_range(start, end)
        if len(report) > _ROW_LIMIT:
            raise NightscoutConnectionError("SGV range exceeds the local row limit")
        rows = {}
        try:
            for entry in report:
                reading = _Reading.parse(entry)
                if not start <= reading.date < end:
                    raise ValueError("SGV outside the completed range")
                existing = rows.get(reading.id)
                if existing is not None and existing != reading:
                    raise ValueError("Conflicting SGVs in the completed range")
                rows[reading.id] = reading
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise NightscoutConnectionError("Invalid completed SGV range") from error
        return rows

    async def _reconcile(
        self, start: int, end: int, now: int
    ) -> tuple[_Snapshot, list[dict[str, Any]]]:
        # Fence first, so stamped writes during the snapshot are replayable.
        fence = await self._source.change_fence()
        rows = await self._read_window(start, end)
        cursor = fence
        changes = []
        if fence is not None:
            changes, cursor = await self._read_changes(fence)
            _apply_changes(rows, changes, start, end)
        return _Snapshot(True, cursor, start, end, now, now, _ordered(rows)), changes

    async def _refresh_legacy(
        self, state: _Snapshot | None, start: int, end: int, now: int
    ) -> _Snapshot:
        # V1 reads give positive observations, with no deletion tombstones.
        # Omitted IDs retain their exact last observed values and dates until expiry.
        rows = {} if state is None else {row.id: row for row in state.rows if start <= row.date < end}
        rows.update(await self._read_window(start, end))
        return _Snapshot(False, None, start, end, now, now, _ordered(rows))

    async def _read_changes(self, after: int) -> tuple[list[dict[str, Any]], int]:
        changes, checkpoint = await self._source.read_changes(after)
        if checkpoint is not None and (type(checkpoint) is not int or checkpoint < after):
            raise NightscoutConnectionError("SGV change cursor did not progress safely")
        for change in changes:
            if (
                not isinstance(change.get("id"), str)
                or not change["id"]
                or type(change.get("modified")) is not int
            ):
                raise NightscoutConnectionError("Invalid SGV change identity or version")
            if not _removes_reading(change):
                try:
                    _Reading.parse(change)
                except (KeyError, TypeError, ValueError, OverflowError) as error:
                    raise NightscoutConnectionError("Invalid SGV change reading") from error
        return changes, after if checkpoint is None else checkpoint

    async def _incremental(
        self, state: _Snapshot, start: int, end: int, now: int, cursor: int
    ) -> tuple[_Snapshot, list[dict[str, Any]]]:
        rows = {row.id: row for row in state.rows if start <= row.date < end}
        if state.end < end:
            # A clock moving forward can admit an already-written measurement.
            # A change feed alone cannot establish coverage of the new tail.
            rows.update(await self._read_window(state.end, end))
        changes, checkpoint = await self._read_changes(cursor)
        _apply_changes(rows, changes, start, end)
        return _Snapshot(True, checkpoint, start, end, now, state.reconciled, _ordered(rows)), changes

    async def async_refresh(self, now: datetime) -> None:
        """Drain changes and commit the result with its matching cursor."""
        checked = _ceil_ms(now)
        start = checked - self._window
        end = checked + _CLOCK_SKEW_MS
        async with self._gate.lock:
            if self._gate.removed:
                raise NightscoutConnectionError("SGV history was removed")
            await self._load_locked()
            state = self._state
            uses_v3 = self._source.uses_v3
            if not uses_v3:
                candidate = await self._refresh_legacy(state, start, end, checked)
                changes = []
            elif (
                state is None
                or not state.uses_v3
                or state.cursor is None
                or checked < state.checked
                or checked - state.reconciled >= _RECONCILE_MS
                or start < state.start
                or state.end < start
            ):
                candidate, changes = await self._reconcile(start, end, checked)
            else:
                try:
                    candidate, changes = await self._incremental(
                        state, start, end, checked, state.cursor
                    )
                except SGVReconciliationRequired:
                    candidate, changes = await self._reconcile(start, end, checked)
            if len(candidate.rows) > _ROW_LIMIT:
                raise NightscoutConnectionError("Retained SGV history exceeds the local row limit")
            cancelled = await self._commit(replace(candidate, revision=self._revision_for(candidate)))
            if state is not None and state.uses_v3 != uses_v3:
                self._overflow.clear()
            else:
                self._update_overflow(changes)
            if cancelled:
                raise asyncio.CancelledError

    def select_current(
        self, now: datetime, bgnow_time: datetime | None, bgnow_value: float | None
    ) -> tuple[datetime, float, str | None] | None:
        """Select the latest glucose with the existing v3 clock/quality rules."""
        state = self._state
        if self._gate.removed or state is None:
            return None
        if now.utcoffset() is None or (bgnow_time is not None and bgnow_time.utcoffset() is None):
            raise ValueError("SGV current timestamps must include a timezone")
        now = now.astimezone(timezone.utc)
        if bgnow_time is not None:
            bgnow_time = bgnow_time.astimezone(timezone.utc)
        earliest = self._local_start(state)
        for reading in reversed(state.rows):
            if reading.date < earliest:
                break
            if reading.sgv <= 39:
                continue
            when = _from_ms(reading.date)
            if when <= now or (
                when - now < _CLOCK_SKEW
                and when == bgnow_time
                and reading.sgv == bgnow_value
            ):
                return when, reading.sgv, reading.direction
        return None

    def _fresh_overflow(self) -> list[_Overflow]:
        now = monotonic()
        return [part for part in self._overflow if part.expires > now]

    def _update_overflow(self, changes: list[dict[str, Any]]) -> None:
        overflow = self._fresh_overflow()
        updated = []
        for part in overflow:
            rows = {row.id: row for row in part.rows}
            _apply_changes(rows, changes, part.start, part.end)
            updated.append(_Overflow(part.start, part.end, part.checked, part.expires, _ordered(rows)))
        if sum(len(part.rows) for part in updated) > _ROW_LIMIT:
            self._overflow.clear()
        else:
            self._overflow = updated

    @staticmethod
    def _remember(overflow: list[_Overflow], part: _Overflow) -> list[_Overflow]:
        """Keep a short, bounded read-through cache independent of retention."""
        combined = []
        for old in overflow:
            if old.end == part.start or part.end == old.start:
                rows = {reading.id: reading for reading in old.rows}
                rows.update({reading.id: reading for reading in part.rows})
                part = _Overflow(
                    min(old.start, part.start),
                    max(old.end, part.end),
                    min(old.checked, part.checked),
                    min(old.expires, part.expires),
                    _ordered(rows),
                )
            else:
                combined.append(old)
        combined.append(part)
        while len(combined) > _OVERFLOW_LIMIT or sum(len(p.rows) for p in combined) > _ROW_LIMIT:
            combined.pop(0)
        return combined

    async def async_range(
        self, start: datetime, end: datetime, *, allow_remote: bool = True
    ) -> dict[str, Any]:
        """Return a half-open range with explicit missing measurement intervals."""
        if (
            start.utcoffset() is None
            or end.utcoffset() is None
            or start.astimezone(timezone.utc) >= end.astimezone(timezone.utc)
        ):
            raise ValueError("SGV range requires an increasing timezone-aware interval")
        lower, upper = _ceil_ms(start), _ceil_ms(end)
        async with self._gate.lock:
            try:
                await self._load_locked()
            except NightscoutConnectionError:
                # An already published generation is still safe to serve.
                pass
            state = None if self._gate.removed else self._state
            allow_remote = allow_remote and not self._gate.removed
            rows = {}
            intervals = []
            checks = []
            if state is not None:
                local_start = self._local_start(state)
                rows.update({row.id: row for row in state.rows if row.date >= local_start})
                intervals.append((local_start, state.end))
                if local_start < upper and lower < state.end:
                    checks.append(state.checked)
            overflow = self._fresh_overflow() if allow_remote else []
            versions = {row_id: state.checked for row_id in rows} if state is not None else {}
            for part in overflow:
                # Include the identities outside the requested span so an edit
                # that moved a reading cannot leave a duplicate inside it.
                additions = {
                    row.id: row for row in part.rows
                    if part.checked >= versions.get(row.id, part.checked)
                }
                merged = rows | additions
                if sum(lower <= row.date < upper for row in merged.values()) > _ROW_LIMIT:
                    continue
                rows = merged
                versions.update(dict.fromkeys(additions, part.checked))
                intervals.append((part.start, part.end))
                if part.start < upper and lower < part.end:
                    checks.append(part.checked)
            for gap_start, gap_end in _missing(lower, upper, intervals):
                if not allow_remote:
                    break
                try:
                    remote = await self._read_window(gap_start, gap_end)
                except _SOURCE_ERRORS:
                    continue
                merged = rows | remote
                if sum(lower <= row.date < upper for row in merged.values()) > _ROW_LIMIT:
                    continue
                rows = merged
                intervals.append((gap_start, gap_end))
                checked = _ceil_ms(dt_util.utcnow())
                checks.append(checked)
                part = _Overflow(
                    gap_start, gap_end, checked, monotonic() + _OVERFLOW_SECONDS, _ordered(remote)
                )
                overflow = self._remember(overflow, part)
            if allow_remote:
                self._overflow = overflow

        gaps = _missing(lower, upper, intervals)
        readings = [row.public() for row in _ordered(rows) if lower <= row.date < upper]
        status = "complete"
        if gaps:
            status = "partial" if checks or readings else "unavailable"
        return {
            "readings": readings,
            "unit": "mg/dL",
            "status": status,
            "missing_ranges": [{"start": lo, "end": hi} for lo, hi in gaps],
            "as_of": _from_ms(min(checks)).isoformat() if checks else None,
        }

    async def async_remove(self) -> None:
        """Delete only the entry's owned cache, after fenced writes finish."""
        async with self._gate.lock:
            try:
                cancelled = await self._finish_executor(self._path.unlink, True)
            except OSError as error:
                self._gate.generation += 1
                raise NightscoutConnectionError("Could not remove SGV history storage") from error
            self._gate.removed = True
            self._gate.generation += 1
            self._overflow.clear()
            self._publish(None)
            if cancelled:
                raise asyncio.CancelledError
