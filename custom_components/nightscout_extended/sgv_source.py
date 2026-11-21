"""Read the Nightscout SGV series and its observable v3 storage changes."""

from __future__ import annotations

import hashlib
import math
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .api import (
    MMOL_PER_MGDL,
    NightscoutClient,
    NightscoutConnectionError,
    _V3_ENTRIES,
    _V3_PAGE_SIZE,
    _V3PageLimitError,
    _num,
    _source_id,
    _text,
    _v3_result,
)

_V1_ENTRIES = "/api/v1/entries.json"
_V3_LAST_MODIFIED = "/api/v3/lastModified"
_V3_HISTORY = f"{_V3_ENTRIES}/history"
# Nightscout's history path parser interprets small numbers as epoch seconds.
_MIN_HISTORY_MS = 946_684_800_000
_MAX_HISTORY_MS = 8_640_000_000_000_000
_MIN_MEASUREMENT_MS = -62_135_596_800_000
_MAX_MEASUREMENT_MS = 253_402_300_799_999
_CHANGE_OVERLAP_MS = 60_000
_HISTORY_LIMIT = 1000
_MAX_HISTORY_PAGES = 256
_V1_PAGE_SIZE = 1000
_V1_TIE_LIMIT = 4000
_MAX_RANGE_PAGES = 1024


class SGVReconciliationRequired(NightscoutConnectionError):
    """HISTORY could not enumerate a boundary safely."""


def _millis(value: Any) -> int | None:
    if type(value) is int or (type(value) is float and value.is_integer()):
        return int(value)
    return None


def _identity(row: dict[str, Any]) -> str:
    # V3 exposes the logical identifier and substitutes the stored _id for
    # old v1 rows. V1 can expose both fields for a v3-originated document.
    key = _source_id(row.get("identifier")) or _source_id(row.get("_id"))
    if key is None:
        raise NightscoutConnectionError("Missing Nightscout SGV source identity")
    return key


def _reading(
    row: dict[str, Any], key: str, modified: int | None
) -> dict[str, Any] | None:
    date = _millis(row.get("date"))
    if date is None or not _MIN_MEASUREMENT_MS <= date <= _MAX_MEASUREMENT_MS:
        return None
    value = _num(row.get("sgv"))
    units = _text(row.get("units"))
    if value is not None and units and units.lower() in ("mmol", "mmol/l"):
        value /= MMOL_PER_MGDL
    if value is None or not math.isfinite(value):
        return None
    return {
        "id": key,
        "date": date,
        "sgv": value,
        "direction": _text(row.get("direction")),
        "modified": modified,
    }


def _merge_snapshot(
    snapshot: dict[str, dict[str, Any]], row: dict[str, Any], date: int, *, v3: bool
) -> None:
    if row["type"] != "sgv":
        return
    key = _identity(row)
    if row.get("isValid") is False:
        return
    modified = _millis(row.get("srvModified"))
    if v3 and modified == date:
        # SEARCH can invent the server clock from the measurement date for
        # unstamped documents. Never use that ambiguous version to reject a
        # real HISTORY update.
        modified = None
    reading = _reading(row, key, modified)
    if reading is None:
        return
    previous = snapshot.get(key)
    if previous is None:
        snapshot[key] = reading
        return
    old_modified = previous["modified"]
    if modified == old_modified:
        if reading != previous:
            raise NightscoutConnectionError("Conflicting Nightscout SGV snapshot versions")
    elif modified is not None and (old_modified is None or modified > old_modified):
        snapshot[key] = reading


class NightscoutSGVSource:
    """Fetch raw SGV data using the configured client's authorized routes."""

    def __init__(self, client: NightscoutClient) -> None:
        self._client = client
        self._history_limit = _HISTORY_LIMIT
        self._search_limit = _V3_PAGE_SIZE
        parts = urlsplit(client.url)
        site = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1].lower(), parts.path, "", ""))
        self.site_id = hashlib.sha256(site.encode()).hexdigest()

    @property
    def uses_v3(self) -> bool:
        """Report the transport mode negotiated by NightscoutClient."""
        return self._client._use_v3

    async def change_fence(self) -> int | None:
        """Get a server-side fence before scanning the measurement window."""
        if not self.uses_v3:
            return None

        try:
            response = await self._client._protected_v3(_V3_LAST_MODIFIED, {})
        except NightscoutConnectionError as error:
            if error.status in (404, 405):
                return None
            raise
        result = _v3_result(response, _V3_LAST_MODIFIED, dict)
        srv_date = _millis(result.get("srvDate"))
        if srv_date is None or not _MIN_HISTORY_MS <= srv_date <= _MAX_HISTORY_MS:
            raise NightscoutConnectionError("Invalid Nightscout server clock")
        # collections.entries mixes measurement timestamps with storage stamps.
        return srv_date

    async def _history_request(self, after_ms: int, limit: int) -> list[dict[str, Any]]:
        response = await self._client._protected_v3(
            f"{_V3_HISTORY}/{after_ms}", {"limit": str(limit)}
        )
        page = _v3_result(response, _V3_HISTORY, list)
        if len(page) > limit or any(not isinstance(row, dict) for row in page):
            raise NightscoutConnectionError("Invalid Nightscout SGV history page")
        return page

    async def _history_page(self, after_ms: int) -> list[dict[str, Any]]:
        try:
            return await self._history_request(after_ms, self._history_limit)
        except NightscoutConnectionError as error:
            # The client's typed limit error covers fixed SEARCH paths.
            # HISTORY has a validated timestamp and only the limit parameter.
            if error.status != 400 or self._history_limit == 1:
                raise

        # Find the largest usable page size so a timestamp tie fits whenever
        # the server and the client bound allow it.
        low, high = 0, self._history_limit
        page = []
        while high - low > 1:
            limit = (low + high) // 2
            try:
                candidate = await self._history_request(after_ms, limit)
            except NightscoutConnectionError as error:
                if error.status != 400 or limit == 1:
                    raise
                high = limit
            else:
                low, page = limit, candidate
        self._history_limit = low
        return page

    async def read_changes(self, after_ms: int) -> tuple[list[dict[str, Any]], int | None]:
        """Read observable changes, including bounded replay of older writes."""
        if not self.uses_v3:
            return [], None
        if type(after_ms) is not int or not _MIN_HISTORY_MS <= after_ms <= _MAX_HISTORY_MS:
            raise NightscoutConnectionError("Invalid Nightscout SGV history cursor")

        boundary = max(_MIN_HISTORY_MS, after_ms - _CHANGE_OVERLAP_MS)
        newest = after_ms
        changes: dict[tuple[int, str], dict[str, Any]] = {}
        for _ in range(_MAX_HISTORY_PAGES):
            page = await self._history_page(boundary)
            previous = boundary
            for row in page:
                modified = _millis(row.get("srvModified"))
                if (
                    modified is None or not boundary < modified <= _MAX_HISTORY_MS
                    or modified < previous
                ):
                    raise NightscoutConnectionError("Unordered Nightscout SGV history page")
                previous = modified
                newest = max(newest, modified)

                kind = _text(row.get("type"))
                if not kind:
                    raise NightscoutConnectionError("Missing Nightscout entry type in history")
                if kind != "sgv":
                    continue
                key = _identity(row)
                if row.get("isValid") is False:
                    change = {"id": key, "modified": modified, "deleted": True}
                else:
                    change = _reading(row, key, modified)
                    if change is None:
                        # A legitimate version can replace an older usable
                        # reading with an unusable measurement.
                        change = {"id": key, "modified": modified, "invalid": True}
                version = (modified, key)
                if changes.setdefault(version, change) != change:
                    raise SGVReconciliationRequired("Conflicting Nightscout SGV history versions")

            if len(page) < self._history_limit:
                return [changes[key] for key in sorted(changes)], newest if newest > after_ms else None
            # /history/<ms> is strictly greater. Repeat the last millisecond
            # so a group split by the page limit can be read in full.
            next_boundary = previous - 1
            if next_boundary <= boundary:
                raise SGVReconciliationRequired("Saturated Nightscout SGV history timestamp")
            boundary = next_boundary

        raise SGVReconciliationRequired("Nightscout SGV history paging bound exceeded")

    async def _v3_range_page(
        self, start: int, end: int, *, skip: int = 0
    ) -> tuple[list[Any], int]:
        while True:
            limit = self._search_limit
            try:
                response = await self._client._protected_v3(
                    _V3_ENTRIES,
                    {
                        "type": "sgv",
                        "date$gte": str(start),
                        "date$lt": str(end),
                        "sort": "date",
                        "limit": str(limit),
                        "skip": str(skip),
                    },
                )
            except _V3PageLimitError:
                if limit == 1 or skip:
                    raise
                self._search_limit = max(1, limit // 2)
            else:
                return _v3_result(response, _V3_ENTRIES, list), limit

    async def _v1_range_page(self, start: int, end: int, limit: int) -> Any:
        return await self._client._get(
            _V1_ENTRIES,
            {
                "find[type]": "sgv",
                "find[date][$gte]": str(start),
                "find[date][$lt]": str(end),
                "count": str(limit),
            },
        )

    async def read_range(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """Read a complete half-open raw snapshot or report a typed failure."""
        v3 = self.uses_v3
        snapshot: dict[str, dict[str, Any]] = {}
        start, end = start_ms, end_ms
        skip = 0
        tie: int | None = None
        seen_tie_ids: set[str] = set()
        for _ in range(_MAX_RANGE_PAGES):
            if start >= end:
                return sorted(snapshot.values(), key=lambda row: (row["date"], row["id"]))

            lower, upper = (start, end) if tie is None else (tie, tie + 1)
            if v3:
                page, limit = await self._v3_range_page(lower, upper, skip=skip)
            else:
                limit = _V1_PAGE_SIZE if tie is None else _V1_TIE_LIMIT
                page = await self._v1_range_page(lower, upper, limit)

            if not isinstance(page, list) or len(page) > limit:
                raise NightscoutConnectionError("Invalid Nightscout SGV range page")
            dates = []
            for row in page:
                if not isinstance(row, dict) or not _text(row.get("type")):
                    raise NightscoutConnectionError("Unexpected Nightscout SGV range entry")
                date = row.get("date")
                if type(date) not in (int, float) or not lower <= date < upper:
                    raise NightscoutConnectionError("Invalid or out-of-range Nightscout SGV date")
                if row["type"] == "sgv":
                    _identity(row)
                bucket = math.floor(date)
                if dates and (bucket < dates[-1] if v3 else bucket > dates[-1]):
                    raise NightscoutConnectionError("Unordered Nightscout SGV range page")
                dates.append(bucket)

            full = len(page) == limit
            if tie is not None:
                # Only a single measurement millisecond needs SEARCH offset
                # paging. Outside that cohort use date bounds so concurrent
                # changes to earlier dates can't shift later pages.
                ids = {_identity(row) for row in page if row["type"] == "sgv"}
                if not v3 and full:
                    raise NightscoutConnectionError("Saturated legacy Nightscout SGV timestamp")
                if ids and not ids.difference(seen_tie_ids):
                    raise NightscoutConnectionError("Non-progressing Nightscout SGV range page")
                seen_tie_ids.update(ids)
                for row, date in zip(page, dates, strict=True):
                    _merge_snapshot(snapshot, row, date, v3=v3)
                if full:
                    skip += limit
                else:
                    if v3:
                        start = tie + 1
                    else:
                        end = tie
                    skip, tie = 0, None
                    seen_tie_ids.clear()
                continue

            if not full:
                for row, date in zip(page, dates, strict=True):
                    _merge_snapshot(snapshot, row, date, v3=v3)
                return sorted(snapshot.values(), key=lambda row: (row["date"], row["id"]))

            boundary = dates[-1]
            for row, date in zip(page, dates, strict=True):
                if date != boundary:
                    _merge_snapshot(snapshot, row, date, v3=v3)
            # Keep the last timestamp in the next request. If that inclusive
            # stamp alone filled this page, fetch the complete isolated cohort.
            if v3 and boundary > start:
                start = boundary
            elif not v3 and boundary < end - 1:
                end = boundary + 1
            else:
                tie = boundary

        raise NightscoutConnectionError("Nightscout SGV range paging bound exceeded")
