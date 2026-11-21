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
_V1_DEVICESTATUS = "/api/v1/devicestatus/"
_V3_DEVICESTATUS = "/api/v3/devicestatus"
_RESERVOIR_MAX_AGE = timedelta(hours=1)
_RESERVOIR_UPLOAD_SKEW = timedelta(minutes=1)
_LEGACY_RESERVOIR_LIMIT = 1000
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


@dataclass(frozen=True)
class ReservoirReading:
    """An AAPS reservoir reading with its pump-contact time and exact uploader label."""

    units: int | float
    source_time: datetime
    uploader: str = field(repr=False)


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
    reservoir: ReservoirReading | None = None

    def last_treatment(self, attr: str, now: datetime) -> Treatment | None:
        """The most recent treatment at or before `now` with a non-zero `attr` (carbs or insulin)."""
        for t in reversed(self.treatments):
            if t.time <= now and getattr(t, attr):
                return t
        return None

    @property
    def forecast_source_time(self) -> datetime | None:
        """The v2 bgnow timestamp used to select an AR2 horizon."""
        return self.glucose_time if self._forecast_anchor is False else self._forecast_anchor

    def forecast_at(self, minutes: int) -> ForecastPoint | None:
        """The forecast point closest to `minutes` after its source reading (within 3 min)."""
        anchor = self.forecast_source_time
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


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def parse_properties(props: dict[str, Any]) -> NightscoutData:
    """Turn a /api/v2/properties response into NightscoutData.

    Every plugin is optional: a Nightscout without `cob` enabled simply leaves cob as None.
    """
    data = NightscoutData()

    bgnow = _object(props.get("bgnow"))
    data.glucose = _num(bgnow.get("last"))
    data.glucose_time = _ms_to_dt(bgnow.get("mills"))

    delta = _object(props.get("delta"))
    data.delta = _num(delta.get("mgdl"))

    direction = _object(props.get("direction"))
    data.direction = _text(direction.get("value")) or None

    iob = _object(props.get("iob"))
    data.iob = _num(iob.get("iob"))
    data.iob_source = _text(iob.get("source"))

    cob = _object(props.get("cob"))
    data.cob = _num(cob.get("cob"))
    data.cob_source = _text(cob.get("source"))

    basal = _object(_object(props.get("basal")).get("current"))
    data.basal = _num(basal.get("totalbasal"))

    predicted = _object(_object(props.get("ar2")).get("forecast")).get("predicted")
    if not isinstance(predicted, list):
        return data
    for point in predicted:
        if not isinstance(point, dict):
            continue
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


def _make_treatment(
    item: dict[str, Any], uid: str | None, when: datetime, *, strict_numbers: bool = False,
) -> Treatment:
    def number(name: str) -> float | None:
        value = item.get(name)
        if strict_numbers and type(value) not in (int, float):
            return None
        return _num(value) or None

    notes = item.get("notes")
    return Treatment(
        uid=uid or str(item.get("_id") or item.get("identifier") or f"{item.get('eventType')}-{when.isoformat()}"),
        time=when,
        event_type=str(item.get("eventType") or "Treatment"),
        carbs=number("carbs"),
        insulin=number("insulin"),
        notes=notes if isinstance(notes, str) and notes.strip() else None,
        entered_by=_text(item.get("enteredBy")),
        duration_min=number("duration"),
    )


def parse_treatments(items: Any) -> list[Treatment]:
    """Turn /api/v1/treatments.json into Treatments, oldest first. Unusable entries are skipped."""
    if not isinstance(items, list):
        return []
    rows = _current_treatment_rows([item for item in items if isinstance(item, dict)])
    treatments = []
    for uid, item in rows:
        when = _parse_time(item)
        if when is not None:
            treatments.append(_make_treatment(item, uid, when))
    treatments.sort(key=lambda treatment: treatment.time)
    return treatments


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


def _reservoir_contact(value: Any) -> datetime | None:
    if not isinstance(value, str) or value[10:11] != "T":
        return None
    try:
        contact = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return contact.astimezone(timezone.utc) if contact.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def select_reservoir(
    rows: list[dict[str, Any]], now: datetime, uploader: str | None,
) -> ReservoirReading | None:
    """Find the newest qualifying AAPS pump contact from one exact uploader."""
    earliest = now - _RESERVOIR_MAX_AGE
    discovered: set[str | None] = set()
    observations: dict[str, list[tuple[datetime, int | float | None]]] = {}
    for row in rows:
        device = _source_id(row.get("device"))
        if row.get("isValid") is False or (uploader is not None and device != uploader):
            continue
        pump = _object(row.get("pump"))
        status = _object(pump.get("status"))
        if "reservoir" not in pump and "timestamp" not in status:
            continue
        discovered.add(device)
        contact = _reservoir_contact(status.get("timestamp"))
        if device is None or contact is None or not earliest <= contact <= now:
            continue
        raw = pump.get("reservoir")
        amount = raw if type(raw) is int or (type(raw) is float and math.isfinite(raw)) else None
        observations.setdefault(device, []).append((contact, amount if amount is not None and amount >= 0 else None))

    if uploader is None and len(discovered) == 1:
        uploader = next(iter(discovered))
    if uploader is None or not (candidates := observations.get(uploader)):
        return None
    latest = max(contact for contact, _ in candidates)
    amounts = {amount for contact, amount in candidates if contact == latest}
    if len(amounts) != 1 or (amount := amounts.pop()) is None:
        return None
    return ReservoirReading(amount, latest, uploader)


def _treatment_identity(row: dict[str, Any]) -> tuple[str, str] | None:
    storage_id = _source_id(row.get("_id"))
    if storage_id is not None:
        return "_id", storage_id
    identifier = _source_id(row.get("identifier"))
    return ("identifier", identifier) if identifier is not None else None


def _current_treatment_rows(
    rows: list[dict[str, Any]],
) -> list[tuple[str | None, dict[str, Any]]]:
    # Preserve the Mongo _id when an observed identifier alias names the same row.
    aliases: dict[str, str | None] = {}
    for row in rows:
        identifier = _source_id(row.get("identifier"))
        storage_id = _source_id(row.get("_id"))
        if identifier is not None and storage_id is not None:
            if aliases.setdefault(identifier, storage_id) != storage_id:
                aliases[identifier] = None  # conflicting pairs cannot identify another row

    chosen: dict[tuple[str, str | int], tuple[tuple[float, bool], str | None, dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        identity = _treatment_identity(row)
        if identity is not None and identity[0] == "identifier":
            storage_id = aliases.get(identity[1])
            if storage_id is not None:
                identity = ("_id", storage_id)
        # An anonymous row is its own occurrence, including at a shared time.
        key = identity if identity is not None else ("row", index)
        uid = identity[1] if identity is not None else None
        modified = _num(row.get("srvModified"))
        deleted = row.get("isValid") is False
        rank = (modified if modified is not None else -math.inf, deleted)
        if key not in chosen or rank > chosen[key][0]:
            chosen[key] = (rank, uid, row)

    return [(uid, row) for (_, deleted), uid, row in chosen.values() if not deleted]


def _merge_v3_treatments(
    rows: list[dict[str, Any]], start: datetime, end: datetime,
) -> list[Treatment]:
    treatments = []
    for uid, row in _current_treatment_rows(rows):
        when = _ms_to_dt(row.get("date")) or _parse_iso(row.get("created_at")) or _ms_to_dt(row.get("mills"))
        if when is not None and start <= when < end:
            treatments.append(_make_treatment(row, uid, when, strict_numbers=True))
    treatments.sort(key=lambda treatment: treatment.time)
    return treatments


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
                    if resp.status == 400 and path in (_V3_ENTRIES, _V3_TREATMENTS, _V3_DEVICESTATUS):
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
                ids = {_treatment_identity(row) for row in page}
                if None in ids:
                    raise NightscoutConnectionError(f"Missing source identity in {_V3_TREATMENTS}")
                if page and not ids.difference(seen):
                    raise NightscoutConnectionError(f"Non-progressing collection page from {_V3_TREATMENTS}")
                seen.update(ids)
                rows.extend(page)
        return _merge_v3_treatments(rows, start, end)

    async def async_get_reservoir(self, now: datetime, uploader: str | None) -> ReservoirReading | None:
        """Read the AAPS upload window, then rank exclusively on pump contact time."""
        # AAPS timestamps the upload after pump contact on the same clock.
        # Allow one minute of upload clock skew without widening contact freshness.
        start = _v3_iso(now - _RESERVOIR_MAX_AGE)
        end = _v3_iso(now + _RESERVOIR_UPLOAD_SKEW)
        if self._use_v3:
            rows = []
            for field in ("date", "created_at"):
                seen: set[str] = set()
                query = {
                    f"{field}$gte": start,
                    f"{field}$lte": end,
                    "sort$desc": field,
                    # Nightscout 15.0.6 prunes response fields by top-level name.
                    # Select only the reservoir and contact time from pump locally.
                    "fields": "identifier,device,pump,isValid",
                }
                async for page in self._v3_pages(_V3_DEVICESTATUS, query, _V3_PAGE_SIZE):
                    ids = {_source_id(row.get("identifier")) for row in page}
                    if None in ids or (page and not ids.difference(seen)):
                        raise NightscoutConnectionError("Non-progressing v3 device-status range")
                    seen.update(identifier for identifier in ids if identifier is not None)
                    rows.extend(page)
        else:
            rows = await self._get(
                _V1_DEVICESTATUS,
                {
                    "find[created_at][$gte]": start,
                    "find[created_at][$lte]": end,
                    "count": str(_LEGACY_RESERVOIR_LIMIT),
                },
            )
            if (
                not isinstance(rows, list) or len(rows) >= _LEGACY_RESERVOIR_LIMIT
                or any(not isinstance(row, dict) for row in rows)
            ):
                raise NightscoutConnectionError("Incomplete legacy device-status range")
        return select_reservoir(rows, now, uploader)

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
