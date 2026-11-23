"""Loopback-only stock 15.0.6 fixture and independent HTTP observation."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection, HTTPException
import json
import re
import socket
import subprocess
from time import monotonic, sleep
from urllib.parse import quote, urlencode
from uuid import uuid4
import warnings

import pytest
import pytest_socket

from .conftest import BASE_URL, MONGO_CONTAINER, NS_CONTAINER
from .stock_contract import PublishedResponseDifference, StockContract

STOCK_VERSION = "15.0.6"
STOCK_IMAGE = (
    "docker.io/nightscout/cgm-remote-monitor:15.0.6@sha256:"
    "868f024f0aaf746aa9c6f639b4add7a78b07bcf4382f9955c5af61d413cad989"
)
MONGO_IMAGE = (
    "docker.io/library/mongo:5.0.32@sha256:"
    "5e3e87afd24d75e722884d777c5713d254f7e88ba65381b5d6484f75a21b73e3"
)
SUBJECTS = "/api/v2/authorization/subjects"
AUTH_REQUEST = "/api/v2/authorization/request"
DEBUG_PERMISSION = "/api/v2/authorization/debug/check"
PROPERTIES = "/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal"
READABLE = ("devicestatus", "entries", "food", "profile", "settings", "treatments")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def require(condition: object, message: str) -> None:
    """Avoid assertion introspection of credential-bearing objects."""
    if not condition:
        raise AssertionError(message)


def millis(value: datetime) -> int:
    return (value - EPOCH) // timedelta(milliseconds=1)


def instant(value: str) -> datetime:
    when = datetime.fromisoformat(value)
    assert when.utcoffset() is not None, "stock timestamp needs its original offset"
    return when.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@contextmanager
def _allow_stock_loopback():
    """Allow one stock exchange even during module fixture setup or teardown."""
    with pytest.MonkeyPatch.context() as restore:
        for attribute in ("socket", "getaddrinfo", "gethostbyname"):
            restore.setattr(socket, attribute, getattr(socket, attribute))
        pytest_socket.enable_socket()
        restore.setattr(socket.socket, "connect", socket.socket.connect)
        pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
        yield


def request(
    path: str, *, headers: Mapping[str, str] | None = None,
    params: Mapping[str, object] | None = None, method: str = "GET", body: object = None,
) -> tuple[int, str, object]:
    """Return decoded observations without redirects, environment proxies, or URL diagnostics."""
    require(BASE_URL == "http://127.0.0.1:1337", "stock fixture must bind to loopback")
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body, allow_nan=False)
    target = f"{path}?{urlencode(params)}" if params else path
    with _allow_stock_loopback():
        connection = HTTPConnection("127.0.0.1", 1337, timeout=10)
        try:
            connection.request(method, target, body=data, headers=request_headers)
            response = connection.getresponse()
            payload = response.read()
            media = response.getheader("Content-Type", "")
            decoded = json.loads(payload) if payload else None
            return response.status, media, decoded
        except (OSError, HTTPException, ValueError) as exc:
            raise AssertionError(f"stock HTTP transport or JSON decoding failed ({type(exc).__name__})") from None
        finally:
            connection.close()


@dataclass(frozen=True, repr=False)
class ReadSubject:
    """Two transports for the same locally provisioned read-only subject."""

    access: str
    bearer: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bearer}"}


@dataclass
class StockReads:
    contract: StockContract
    reported_differences: set[str] = field(default_factory=set)

    def exchange(
        self, path: str, *, subject: ReadSubject | None = None,
        params: Mapping[str, object] | None = None, expected_status: int = 200,
        contract_path: str | None = None, legacy_query: bool = False,
    ) -> object:
        request_params = dict(params or {})
        if legacy_query:
            require(subject is not None, "legacy query requires a read subject")
            request_params["token"] = subject.access
        status, media, body = request(
            path, headers=subject.headers if subject and not legacy_query else None,
            params=request_params,
        )
        route = contract_path or path
        checked_params = dict(params or {})
        if legacy_query:
            checked_params["token"] = "<present>"
        self._validate(route, status, media, body, checked_params)
        assert status == expected_status, f"{route} returned HTTP {status}, expected {expected_status}"
        return body

    def attest_legacy_subject(self, reader: ReadSubject, name: str) -> None:
        """Legacy status echoes the named subject only when the query credential resolves."""
        path = "/api/v1/status.json"
        status, media, body = request(path, params={"token": reader.access})
        require(status == 200 and isinstance(body, dict), "legacy identity check failed")
        authorization = body.get("authorized")
        require(
            isinstance(authorization, dict) and authorization.get("sub") == name
            and isinstance(authorization.get("token"), str) and bool(authorization["token"]),
            "legacy status did not authenticate the named readable subject",
        )
        sanitized = {**body, "authorized": {**authorization, "token": "<present>"}}
        self._validate(path, status, media, sanitized, {"token": "<present>"})

    def _validate(self, route: str, status: int, media: str, body: object, params: Mapping[str, object]) -> None:
        try:
            self.contract.validate_exchange(
                "GET", route, status=status, content_type=media, body=body, params=params,
            )
        except PublishedResponseDifference as exc:
            differences = {
                f"{exc.api} {exc.path} (HTTP {exc.status}): {detail}" for detail in exc.diagnostics
            }
            fresh = differences - self.reported_differences
            if fresh:
                self.reported_differences.update(fresh)
                warnings.warn(
                    "Nightscout stock response differs from published Swagger\n" + "\n".join(sorted(fresh)),
                    stacklevel=2,
                )


def _matching_subject(stock, name: str) -> dict | None:
    status, _, rows = request(SUBJECTS, headers=stock.setup_headers)
    require(status == 200 and isinstance(rows, list), "stock subject listing failed")
    matches = [row for row in rows if isinstance(row, dict) and row.get("name") == name]
    require(len(matches) <= 1, "stock subject name was not unique")
    return matches[0] if matches else None


def read_subject(stock, name: str, contract: StockContract) -> ReadSubject:
    row = _matching_subject(stock, name)
    require(row is not None and row.get("roles") == ["readable"], "stock subject lacks the exact readable role")
    access = row.get("accessToken")
    require(isinstance(access, str) and bool(access), "stock subject has no access credential")
    status, media, exchange = request(f"{AUTH_REQUEST}/{quote(access, safe='')}")
    require(status == 200 and isinstance(exchange, dict), "stock subject exchange failed")
    bearer = exchange.get("token")
    issued, expires = exchange.get("iat"), exchange.get("exp")
    require(
        exchange.get("sub") == name and isinstance(bearer, str) and bool(bearer)
        and type(issued) is int and type(expires) is int and 0 < issued < expires,
        "stock exchange did not return an expiring credential for the selected subject",
    )
    contract.validate_exchange(
        "GET", f"{AUTH_REQUEST}/<subject>", status=status, content_type=media,
        body={**exchange, "token": "<present>"},
    )
    return ReadSubject(access, bearer)


@contextmanager
def named_read_subject(stock) -> Iterator[str]:
    name = f"parity-read-{uuid4().hex}"
    require(_matching_subject(stock, name) is None, "stock subject name must be new")
    try:
        status, _, _ = request(
            SUBJECTS, method="POST", headers=stock.setup_headers, body={"name": name, "roles": ["readable"]},
        )
        require(status == 200, f"stock reader provisioning failed (HTTP {status})")
        yield name
    finally:
        row = _matching_subject(stock, name)
        if row is not None:
            ident = row.get("_id")
            require(isinstance(ident, str) and re.fullmatch(r"[a-f0-9]{24}", ident), "stock subject ID is invalid")
            status, _, _ = request(f"{SUBJECTS}/{ident}", method="DELETE", headers=stock.setup_headers)
            require(status == 200, f"stock subject removal failed (HTTP {status})")
            require(_matching_subject(stock, name) is None, "stock subject removal did not persist")


def _docker(*args: str) -> str:
    try:
        done = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=45, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError(f"disposable stock Docker action failed ({type(exc).__name__})") from None
    require(done.returncode == 0, f"disposable stock Docker action failed: {args[0]}, exit {done.returncode}")
    return done.stdout.strip()


def _assert_disposable() -> None:
    inspect = '{{.Config.Image}} {{index .Config.Labels "org.nightscout.parity.disposable"}}'
    for name, image in ((NS_CONTAINER, STOCK_IMAGE), (MONGO_CONTAINER, MONGO_IMAGE)):
        require(_docker("inspect", "--type", "container", "--format", inspect, name) == f"{image} true",
                f"{name} is not the pinned disposable parity container")


def await_stock(predicate, phase: str, timeout: int = 120) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            accepted = predicate()
        except AssertionError:
            accepted = False
        if accepted:
            return
        sleep(1)
    raise AssertionError(f"disposable stock did not reach {phase} within {timeout}s")


@contextmanager
def stopped_stock() -> Iterator[None]:
    """Flush Nightscout's incremental in-memory caches across fixture transitions."""
    _assert_disposable()
    try:
        _docker("stop", NS_CONTAINER)
        yield
    finally:
        _docker("start", NS_CONTAINER)
        await_stock(lambda: request("/api/v1/status.json")[0] == 200, "HTTP readiness")


def _mongo(stock, statement: str) -> None:
    completed = "version parity: Mongo operation complete"
    try:
        output = stock.mongo(
            '(function () { assert.eq(db.getName(), "nightscout");\n'
            + statement + f'\nprint("{completed}"); }})();\n'
        )
    except RuntimeError:
        raise AssertionError("disposable stock Mongo fixture operation failed") from None
    require(completed in output.splitlines(), "disposable stock Mongo fixture operation did not complete")


def _literal(value: object) -> str:
    """Serialize only generated synthetic fixture data for the existing Mongo shell."""
    return json.dumps(value, allow_nan=False)


def _owned(collection: str, rows: list[dict]) -> str:
    ids = _literal([row["_id"] for row in rows])
    return (
        f'assert(db.getCollection("{collection}").deleteMany('
        f'{{_id: {{$in: {ids}.map(x => ObjectId(x))}}}}).acknowledged);'
    )


def _insert(collection: str, rows: list[dict]) -> str:
    data = _literal(rows)
    return (
        f'assert(db.getCollection("{collection}").insertMany('
        f'{data}.map(x => Object.assign(x, {{_id: ObjectId(x._id)}}))).acknowledged);'
    )


def _entries_namespace(backup: str) -> str:
    require(re.fullmatch(r"parity_parked_entries_[a-f0-9]{32}", backup), "invalid stock scratch namespace")
    return f'db.getCollectionInfos({{name: "{backup}"}}).length'


@contextmanager
def seeded_collections(stock, documents: dict[str, list[dict]], alternate_entries: list[dict]) -> Iterator[None]:
    """Isolate SGV worlds and preserve the other tests' existing entries and indexes."""
    backup = f"parity_parked_entries_{uuid4().hex}"
    namespace = _entries_namespace(backup)
    reserved = False
    all_entries = documents["entries"] + alternate_entries
    try:
        with stopped_stock():
            _mongo(stock, f'assert.eq({namespace}, 0); '
                          'assert.eq(db.getCollectionInfos({name: "entries"}).length, 1);')
            reserved = True
            _mongo(stock, f'assert.commandWorked(db.entries.renameCollection("{backup}", false));')
            for collection, rows in documents.items():
                _mongo(stock, _insert(collection, rows))
        yield
    finally:
        if reserved:
            with stopped_stock():
                _mongo(stock, f'''
                    if ({namespace} === 1) {{
                        var ids = {_literal([row["_id"] for row in all_entries])}.map(x => ObjectId(x));
                        assert.eq(db.entries.countDocuments({{_id: {{$nin: ids}}}}), 0,
                                  "scratch entries contains a nonfixture row");
                        if (db.getCollectionInfos({{name: "entries"}}).length === 1) {{
                            assert(db.entries.drop(), "could not remove owned scratch entries");
                        }}
                        assert.commandWorked(db.getCollection("{backup}").renameCollection("entries", false));
                    }}
                ''')
                for collection, rows in documents.items():
                    if collection != "entries":
                        _mongo(stock, _owned(collection, rows))


@contextmanager
def alternate_seed_rows(stock, collection: str, current: list[dict], alternate: list[dict]) -> Iterator[None]:
    """Temporarily replace only known fixture IDs and restore on failure."""
    replacement = _insert(collection, alternate) if alternate else ""
    try:
        with stopped_stock():
            _mongo(stock, _owned(collection, current) + replacement)
        yield
    finally:
        with stopped_stock():
            _mongo(stock, _owned(collection, current + alternate) + _insert(collection, current))


def synthetic_data(anchor: datetime) -> tuple[dict[str, list[dict]], list[dict]]:
    """Adapt PR2's stock_native.py seed to isolated PR10 fixture IDs and current profiles."""
    def oid() -> str:
        return uuid4().hex[:24]

    def entry(when: datetime, value: int, direction: str) -> dict:
        return {
            "_id": oid(), "type": "sgv", "date": millis(when), "dateString": iso(when),
            "sgv": value, "direction": direction, "device": "version-parity-cgm",
        }

    def treatment(when: datetime, event: str, notes: str, **values: object) -> dict:
        return {
            "_id": oid(), "created_at": iso(when), "date": millis(when), "eventType": event,
            "notes": notes, "enteredBy": "version-parity", **values,
        }

    readings = [
        entry(anchor - timedelta(days=5), 401, "Flat"),
        entry(anchor - timedelta(minutes=10), 157, "Flat"),
        entry(anchor - timedelta(minutes=5), 168, "SingleUp"),
        entry(anchor, 180, "FortyFiveUp"),
    ]
    treatments = [
        treatment(anchor - timedelta(hours=100), "Insulin Change", "older maintenance"),
        treatment(anchor - timedelta(hours=96), "Pump Battery Change", "battery maintenance"),
        treatment(anchor - timedelta(hours=60), "Site Change", "site maintenance"),
        treatment(anchor - timedelta(hours=40), "Insulin Change", "insulin maintenance"),
        treatment(anchor - timedelta(hours=20), "Sensor Change", "sensor maintenance"),
        treatment(anchor - timedelta(minutes=90), "Temp Basal", "basal event", duration=30, rate=1.25, absolute=1.25),
        treatment(anchor - timedelta(minutes=55), "Correction Bolus", "synthetic correction", carbs=0, insulin=1.25),
        treatment(anchor - timedelta(minutes=48), "Snack Bolus", "synthetic snack", carbs=14.5),
        treatment(anchor - timedelta(minutes=46), "Note", "numeric text", carbs="0"),
        treatment(anchor + timedelta(minutes=25), "Note", "future appointment", duration=12),
    ]
    devices = []
    for minutes, reservoir in ((4, 17.5), (2, 0)):
        contact = anchor - timedelta(minutes=minutes)
        uploaded = contact + timedelta(seconds=45)
        devices.append({
            "_id": oid(), "device": "version-parity-pump", "date": millis(uploaded), "created_at": iso(uploaded),
            "pump": {"reservoir": reservoir, "status": {"timestamp": iso(contact)}},
        })
    devices[-1]["loop"] = {
        "iob": {"iob": 1.375, "timestamp": devices[-1]["created_at"]},
        "cob": {"cob": 7.5, "timestamp": devices[-1]["created_at"]},
    }
    profile_at = anchor - timedelta(days=7)
    profile = {
        "_id": oid(), "date": millis(profile_at), "created_at": iso(profile_at), "startDate": iso(profile_at),
        "defaultProfile": "Synthetic", "units": "mg/dl",
        "store": {"Synthetic": {
            "dia": 3, "carbs_hr": 20, "units": "mg/dl", "timezone": "Etc/UTC",
            **{key: [{"time": "00:00", "value": value, "timeAsSeconds": 0}]
               for key, value in {"basal": 0.85, "sens": 48, "carbratio": 10,
                                  "target_low": 90, "target_high": 110}.items()},
        }},
    }
    alternate = [entry(anchor - timedelta(minutes=90), 148, "Flat"), entry(anchor, 10, "Flat")]
    return {"entries": readings, "treatments": treatments, "devicestatus": devices, "profile": [profile]}, alternate
