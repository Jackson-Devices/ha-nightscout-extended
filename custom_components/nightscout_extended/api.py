"""Nightscout API client and parsing.

Kept free of Home Assistant imports so the parsing can be tested on its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import time
from typing import Any, Literal
from urllib.parse import quote

import aiohttp
from yarl import URL

MMOL_PER_MGDL = 1 / 18.0182

PROPERTIES = "bgnow,delta,direction,iob,cob,ar2,basal"
_V1_TREATMENTS = "/api/v1/treatments.json"
_TREATMENT_LIMIT = 1000
_V3_VERSION = "/api/v3/version"
_V3_ENTRIES = "/api/v3/entries"
_V3_TREATMENTS = "/api/v3/treatments"
_V3_PAGE_SIZE = 100
_V3_MAX_PAGES = 100
_V3_SGV_CLOCK_SKEW = timedelta(seconds=60)


class NightscoutAuthError(Exception):
    """The URL answered but rejected the credentials."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NightscoutConnectionError(Exception):
    """Nightscout could not be reached or answered with something unexpected."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _V3PageLimitError(NightscoutConnectionError):
    """The requested v3 page size exceeds the configured server maximum."""


@dataclass(frozen=True)
class _Bearer:
    token: str = field(repr=False)
    expires: float


@dataclass
class ForecastPoint:
    time: datetime
    mgdl: float


@dataclass
class Treatment:
    """A Nightscout treatment: a meal, a dose, a note, or anything else Care Portal can log."""

    uid: str
    time: datetime
    event_type: str
    carbs: float | None = None
    insulin: float | None = None
    notes: str | None = None
    entered_by: str | None = None
    duration_min: float | None = None

    @property
    def summary(self) -> str:
        parts = []
        if self.carbs:
            parts.append(f"{self.carbs:g} g carbs")
        if self.insulin:
            parts.append(f"{self.insulin:g} U insulin")
        if parts:
            return f"{self.event_type}: {', '.join(parts)}"
        if self.notes:
            return self.notes.strip().splitlines()[0][:120]
        return self.event_type


@dataclass
class NightscoutData:
    """One poll's worth of values. Glucose values are always mg/dL here."""

    glucose: float | None = None
    glucose_time: datetime | None = None
    delta: float | None = None
    direction: str | None = None
    iob: float | None = None
    iob_source: str | None = None
    cob: float | None = None
    cob_source: str | None = None
    basal: float | None = None
    forecast: list[ForecastPoint] = field(default_factory=list)
    treatments: list[Treatment] = field(default_factory=list)  # sorted by time, past and future
    _forecast_anchor: datetime | None | Literal[False] = field(default=False, init=False, repr=False)

    def last_treatment(self, attr: str, now: datetime) -> Treatment | None:
        """The most recent treatment at or before `now` with a non-zero `attr` (carbs or insulin)."""
        for t in reversed(self.treatments):
            if t.time <= now and getattr(t, attr):
                return t
        return None

    def forecast_at(self, minutes: int) -> ForecastPoint | None:
        """The forecast point closest to `minutes` after the latest reading (within 3 min)."""
        anchor = self.glucose_time if self._forecast_anchor is False else self._forecast_anchor
        if not self.forecast or anchor is None:
            return None
        target = anchor.timestamp() + minutes * 60
        best = min(self.forecast, key=lambda p: abs(p.time.timestamp() - target))
        return best if abs(best.time.timestamp() - target) <= 180 else None


def _ms_to_dt(ms: Any) -> datetime | None:
    value = _num(ms)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def parse_properties(props: dict[str, Any]) -> NightscoutData:
    """Turn a /api/v2/properties response into NightscoutData.

    Every plugin is optional: a Nightscout without `cob` enabled simply leaves cob as None.
    """
    data = NightscoutData()

    bgnow = props.get("bgnow") or {}
    data.glucose = _num(bgnow.get("last"))
    data.glucose_time = _ms_to_dt(bgnow.get("mills"))

    delta = props.get("delta") or {}
    data.delta = _num(delta.get("mgdl"))

    direction = props.get("direction") or {}
    data.direction = direction.get("value") or None

    iob = props.get("iob") or {}
    data.iob = _num(iob.get("iob"))
    data.iob_source = iob.get("source")

    cob = props.get("cob") or {}
    data.cob = _num(cob.get("cob"))
    data.cob_source = cob.get("source")

    basal = (props.get("basal") or {}).get("current") or {}
    data.basal = _num(basal.get("totalbasal"))

    predicted = ((props.get("ar2") or {}).get("forecast") or {}).get("predicted") or []
    for point in predicted:
        when, mgdl = _ms_to_dt(point.get("mills")), _num(point.get("mgdl"))
        if when is not None and mgdl is not None:
            data.forecast.append(ForecastPoint(when, mgdl))
    data.forecast.sort(key=lambda p: p.time)
    return data


def _parse_time(item: dict[str, Any]) -> datetime | None:
    created = item.get("created_at")
    if isinstance(created, str) and created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _ms_to_dt(item.get("mills") or item.get("date"))


def parse_treatments(items: Any) -> list[Treatment]:
    """Turn /api/v1/treatments.json into Treatments, oldest first. Unusable entries are skipped."""
    out: list[Treatment] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        when = _parse_time(item)
        if when is None:
            continue
        notes = item.get("notes")
        out.append(
            Treatment(
                uid=str(item.get("_id") or item.get("identifier") or f"{item.get('eventType')}-{when.isoformat()}"),
                time=when,
                event_type=str(item.get("eventType") or "Treatment"),
                carbs=_num(item.get("carbs")) or None,
                insulin=_num(item.get("insulin")) or None,
                notes=notes if isinstance(notes, str) and notes.strip() else None,
                entered_by=item.get("enteredBy") if isinstance(item.get("enteredBy"), str) else None,
                duration_min=_num(item.get("duration")) or None,
            )
        )
    out.sort(key=lambda t: t.time)
    return out


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _v3_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


def _v3_result(payload: Any, path: str, kind: type) -> Any:
    if isinstance(payload, dict) and payload.get("status") == 200 and isinstance(payload.get("result"), kind):
        return payload["result"]
    raise NightscoutConnectionError(f"Unexpected v3 response from {path}")


def _source_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _treatment_id(row: dict[str, Any]) -> str | None:
    return _source_id(row.get("_id")) or _source_id(row.get("identifier"))


def _merge_v3_treatments(
    rows: list[dict[str, Any]], start: datetime, end: datetime,
) -> list[Treatment]:
    # v1 exposes Mongo's _id as the HA event ID. A v3 copy of the same row
    # can carry both IDs, so use the observed pair to reconcile its other copy.
    aliases = {
        row["identifier"]: row["_id"]
        for row in rows
        if _source_id(row.get("identifier")) and _source_id(row.get("_id"))
    }
    chosen: dict[str, tuple[tuple[float, bool], dict[str, Any]]] = {}
    for row in rows:
        identifier = _source_id(row.get("identifier"))
        uid = _source_id(row.get("_id")) or aliases.get(identifier, identifier)
        modified = _num(row.get("srvModified"))
        deleted = row.get("isValid") is False
        rank = (modified if modified is not None else -math.inf, deleted)
        if uid is not None and (uid not in chosen or rank > chosen[uid][0]):
            chosen[uid] = (rank, row)

    normalized = []
    for uid, ((_, deleted), row) in chosen.items():
        if deleted:
            continue
        when = _ms_to_dt(row.get("date")) or _parse_iso(row.get("created_at")) or _ms_to_dt(row.get("mills"))
        if when is not None and start <= when < end:
            normalized.append({**row, "_id": uid, "created_at": when.isoformat()})
    return parse_treatments(normalized)


def _apply_v3_glucose(
    data: NightscoutData, sample: tuple[datetime, float, str | None] | None,
) -> None:
    data._forecast_anchor = data.glucose_time
    if sample is None:
        data.glucose = data.glucose_time = data.delta = data.direction = None
        return
    when, glucose, direction = sample
    if data.glucose_time != when or data.glucose is None or not math.isclose(data.glucose, glucose, abs_tol=0.05):
        data.delta = None
        data.direction = direction
    data.glucose, data.glucose_time = glucose, when


class NightscoutClient:
    """Read legacy data or a mix of v3 collections and calculated v2 properties."""

    def __init__(self, session: aiohttp.ClientSession, url: str, credential: str | None) -> None:
        self._session = session
        self.url = url.rstrip("/")
        self._credential = (credential or "").strip() or None
        self._use_secret = False
        self._use_v3 = False
        self._bearer: _Bearer | None = None
        self._bearer_lock = asyncio.Lock()

    async def _request(
        self, path: str, query: dict[str, str] | None = None,
        *, headers: dict[str, str] | None = None, endpoint: str | None = None,
    ) -> Any:
        # Use static endpoint labels. Exceptions, redirect locations, and error bodies
        # can echo auth headers, the legacy query token, or the exchange path.
        label = endpoint or path
        try:
            async with self._session.get(
                URL(f"{self.url}{path}", encoded=True),
                headers={"Accept": "application/json", **(headers or {})},
                params=query, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False,
            ) as resp:
                if resp.status in (401, 403):
                    raise NightscoutAuthError(f"HTTP {resp.status} from {label}", status=resp.status)
                if 300 <= resp.status < 400:
                    raise NightscoutConnectionError(f"{label} redirected (HTTP {resp.status})")
                if resp.status != 200:
                    if resp.status == 400 and path in (_V3_ENTRIES, _V3_TREATMENTS):
                        try:
                            body = await resp.json(content_type=None)
                        except ValueError:
                            body = None
                        if isinstance(body, dict) and body.get("message") == "Parameter limit out of tolerance":
                            raise _V3PageLimitError(f"HTTP 400 from {label}: page limit rejected", status=400)
                    raise NightscoutConnectionError(f"HTTP {resp.status} from {label}", status=resp.status)
                try:
                    return await resp.json(content_type=None)
                except ValueError:
                    raise NightscoutConnectionError(f"Not JSON from {label}") from None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise NightscoutConnectionError(f"Request failed for {label}") from None

    async def _get(
        self, path: str, query: dict[str, str] | None = None, *, use_secret: bool | None = None,
    ) -> Any:
        params = dict(query or {})
        headers = {}
        if self._credential:
            use_secret = self._use_secret if use_secret is None else use_secret
            if use_secret:
                headers["api-secret"] = hashlib.sha1(self._credential.encode()).hexdigest()
            else:
                params["token"] = self._credential
        return await self._request(path, params, headers=headers)

    async def _legacy_status(self, use_secret: bool) -> dict[str, Any]:
        status = await self._get("/api/v1/status.json", use_secret=use_secret)
        if not isinstance(status, dict) or not isinstance(status.get("settings"), dict):
            raise NightscoutConnectionError("Unexpected /api/v1/status.json response")
        # Some installations serve status without authentication.
        await self._get("/api/v2/properties/bgnow", use_secret=use_secret)
        return status

    async def async_get_status(self) -> dict[str, Any]:
        """Check site metadata and establish the read mode for subsequent requests."""
        try:
            status = await self._legacy_status(self._use_secret)
        except NightscoutAuthError as raw_error:
            if not self._credential or self._use_secret or self._use_v3:
                raise
            try:
                status = await self._legacy_status(True)
            except NightscoutAuthError:
                raise raw_error from None
            self._use_secret = True
        if self._credential and not self._use_secret and not self._use_v3:
            await self._select_v3()
        return status

    async def _select_v3(self) -> None:
        try:
            version = _v3_result(await self._request(_V3_VERSION), _V3_VERSION, dict)
        except NightscoutConnectionError as err:
            if err.status in (404, 405):
                return
            raise
        api_version = version.get("apiVersion")
        if not isinstance(api_version, str) or not api_version.startswith("3."):
            raise NightscoutConnectionError(f"Unexpected API version from {_V3_VERSION}")
        try:
            bearer = await self._exchange_bearer()
        except NightscoutConnectionError as err:
            if err.status in (404, 405):
                return
            raise
        except NightscoutAuthError as err:
            # Old deployments can allow the raw token on the two proved legacy routes
            # without accepting it as a subject at the exchange endpoint.
            if err.status == 401:
                return
            raise
        self._bearer = bearer
        self._use_v3 = True

    async def _exchange_bearer(self) -> _Bearer:
        label = "Nightscout authorization exchange"
        subject = quote(self._credential or "", safe="")
        payload = await self._request(f"/api/v2/authorization/request/{subject}", endpoint=label)
        if isinstance(payload, dict):
            token = payload.get("token")
            issued, expires = payload.get("iat"), payload.get("exp")
            if (
                isinstance(token, str) and token and not any(char.isspace() for char in token)
                and type(issued) in (int, float) and type(expires) in (int, float)
                and _num(issued) is not None and _num(expires) is not None and issued < expires
            ):
                return _Bearer(token, expires)
        raise NightscoutConnectionError(f"Unexpected response from {label}")

    async def _get_bearer(self, rejected: _Bearer | None = None) -> _Bearer:
        async with self._bearer_lock:
            current = self._bearer
            if current is not None and current is not rejected and current.expires > time():
                return current
            current = await self._exchange_bearer()
            if current.expires <= time():
                raise NightscoutConnectionError("Nightscout authorization exchange returned an expired token")
            self._bearer = current
            return current

    async def _protected_v3(self, path: str, query: dict[str, str]) -> Any:
        bearer = await self._get_bearer()
        try:
            return await self._request(path, query, headers={"Authorization": f"Bearer {bearer.token}"})
        except NightscoutAuthError as err:
            if err.status != 401:
                raise
        bearer = await self._get_bearer(bearer)
        try:
            return await self._request(path, query, headers={"Authorization": f"Bearer {bearer.token}"})
        except NightscoutAuthError as err:
            if err.status == 401 and self._bearer is bearer:
                self._bearer = None
            raise

    async def _v3_pages(self, path: str, query: dict[str, str], limit: int) -> AsyncIterator[list[dict[str, Any]]]:
        if limit <= 0:
            raise NightscoutConnectionError(f"Invalid page limit for {path}")
        size = min(limit, _V3_PAGE_SIZE)
        skip = 0
        for _ in range(_V3_MAX_PAGES):
            while True:
                try:
                    payload = await self._protected_v3(path, {**query, "limit": str(size), "skip": str(skip)})
                except _V3PageLimitError:
                    if skip or size == 1:
                        raise
                    size = max(1, size // 2)
                else:
                    break
            page = _v3_result(payload, path, list)
            if len(page) > size or any(not isinstance(row, dict) for row in page):
                raise NightscoutConnectionError(f"Invalid collection page from {path}")
            yield page
            if len(page) < size:
                return
            skip += size
        raise NightscoutConnectionError(f"Collection paging bound exceeded for {path}")

    async def _v3_glucose(
        self, now: datetime, bgnow_time: datetime | None, bgnow_glucose: float | None,
    ) -> tuple[datetime, float, str | None] | None:
        # Nightscout has already checked bgnow against its own clock. Allow HA's
        # clock to trail it slightly, and corroborate the exact reading below.
        future_bgnow = None
        if (
            bgnow_time is not None and bgnow_glucose is not None
            and timedelta(0) < bgnow_time - now < _V3_SGV_CLOCK_SKEW
        ):
            future_bgnow = bgnow_time
        query = {"type": "sgv", "date$lte": _v3_iso(future_bgnow or now), "sort$desc": "date"}
        async for page in self._v3_pages(_V3_ENTRIES, query, _V3_PAGE_SIZE):
            candidates = []
            for row in page:
                if row.get("type") != "sgv" or row.get("isValid") is False:
                    continue
                when = _ms_to_dt(row.get("date")) or _parse_iso(row.get("dateString"))
                value = _num(row.get("sgv"))
                if when is None or value is None:
                    continue
                units = row.get("units")
                if isinstance(units, str) and units.lower() in ("mmol", "mmol/l"):
                    value /= MMOL_PER_MGDL
                if when > now and (when != future_bgnow or value != bgnow_glucose):
                    continue
                # Nightscout's bgnow excludes CGM error and sentinel readings.
                if math.isfinite(value) and value > 39:
                    direction = row.get("direction")
                    candidates.append((when, value, direction if isinstance(direction, str) else None))
            if candidates:
                return max(candidates, key=lambda sample: sample[0])
        return None

    async def _v3_treatments(
        self, start: datetime, end: datetime, limit: int, *, include_end_ms: bool = False,
    ) -> list[Treatment]:
        # The server filters stored values before reading/normalizing them. Older
        # documents can have created_at and no v3 date in storage.
        rows = []
        end_predicate = "lte" if include_end_ms else "lt"
        for key in ("date", "created_at"):
            query = {f"{key}$gte": _v3_iso(start), f"{key}${end_predicate}": _v3_iso(end), "sort$desc": key}
            seen = set()
            async for page in self._v3_pages(_V3_TREATMENTS, query, limit):
                ids = {_treatment_id(row) for row in page}
                if None in ids:
                    raise NightscoutConnectionError(f"Missing source identity in {_V3_TREATMENTS}")
                if page and not ids.difference(seen):
                    raise NightscoutConnectionError(f"Non-progressing collection page from {_V3_TREATMENTS}")
                seen.update(ids)
                rows.extend(page)
        return _merge_v3_treatments(rows, start, end)

    async def _legacy_treatment_range(self, start: str, end: str, limit: int) -> Any:
        return await self._get(
            _V1_TREATMENTS,
            {"find[created_at][$gte]": start, "find[created_at][$lte]": end, "count": str(limit)},
        )

    async def async_get_treatments(self, start: datetime, end: datetime, limit: int = _TREATMENT_LIMIT) -> list[Treatment]:
        if self._use_v3:
            return await self._v3_treatments(start, end, limit)
        return parse_treatments(await self._legacy_treatment_range(_iso(start), _iso(end), limit))

    async def _calendar_lookback(self, default_length: timedelta, before: datetime) -> timedelta:
        minimum_minutes = default_length.total_seconds() / 60
        if self._use_v3:
            payload = await self._protected_v3(
                _V3_TREATMENTS,
                {
                    "duration$gt": f"{minimum_minutes:g}", "sort$desc": "duration",
                    "limit": "1", "skip": "0",
                },
            )
            rows = _v3_result(payload, _V3_TREATMENTS, list)
        else:
            # Older v1 servers leave duration comparison operands as strings.
            # The Mongo driver coerces a sort tuple's direction to a number.
            rows = await self._get(
                _V1_TREATMENTS,
                {
                    "find[created_at][$lt]": _v3_iso(before),
                    "find[duration][$type]": "number",
                    "sort[0][0]": "duration",
                    "sort[0][1]": "desc",
                    "count": "1",
                },
            )
        if isinstance(rows, list):
            if not rows:
                return default_length
            if len(rows) == 1 and isinstance(rows[0], dict):
                raw = rows[0].get("duration")
                duration = _num(raw) if type(raw) in (int, float) else None
                if duration is not None:
                    if duration > minimum_minutes:
                        try:
                            return timedelta(minutes=duration)
                        except OverflowError:
                            pass
                    elif not self._use_v3:
                        return default_length
        raise NightscoutConnectionError("Invalid maximum treatment duration from Nightscout")

    async def async_get_calendar_treatments(
        self, start: datetime, end: datetime, default_length: timedelta,
    ) -> list[Treatment]:
        """Fetch candidate events, including treatments that began before the displayed range."""
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if start >= end:
            return []

        since_first_time = start - datetime.min.replace(tzinfo=timezone.utc)
        lookback = min(since_first_time, default_length)
        if lookback < since_first_time:
            # The maximum duration gives a safe lower bound for an overlap.
            lookback = await self._calendar_lookback(default_length, start - lookback)
        earliest = start - min(since_first_time, lookback)

        if self._use_v3:
            # Nightscout stores milliseconds. An unaligned upper bound may also
            # include its floor millisecond; local filtering still uses the exact end.
            return await self._v3_treatments(
                earliest, end, _TREATMENT_LIMIT, include_end_ms=bool(end.microsecond % 1000),
            )
        rows = await self._legacy_treatment_range(_v3_iso(earliest), _v3_iso(end), _TREATMENT_LIMIT)
        if not isinstance(rows, list) or len(rows) >= _TREATMENT_LIMIT:
            raise NightscoutConnectionError("Incomplete legacy Nightscout calendar range")
        return parse_treatments(rows)

    async def async_get_data(
        self, past: timedelta = timedelta(hours=48), ahead: timedelta = timedelta(days=62)
    ) -> NightscoutData:
        props = await self._get(f"/api/v2/properties/{PROPERTIES}")
        if not isinstance(props, dict):
            raise NightscoutConnectionError("Unexpected properties response")
        data = parse_properties(props)
        now = datetime.now(timezone.utc)
        if self._use_v3:
            _apply_v3_glucose(data, await self._v3_glucose(now, data.glucose_time, data.glucose))
        data.treatments = await self.async_get_treatments(now - past, now + ahead)
        return data

    @property
    def uses_api_secret(self) -> bool:
        return self._use_secret

    def set_uses_api_secret(self, value: bool) -> None:
        self._use_secret = value
        if value:
            self._use_v3 = False
            self._bearer = None
