"""Validate Nightscout 15.0.6 stock GETs using the running image's own Swagger JSON."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from urllib.parse import quote

from openapi_schema_validator import OAS30ReadValidator, oas30_format_checker
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT4


STOCK_COMMIT = "9cd304f78a5c12401c9711cbd56d2a12eaca0632"


@dataclass(frozen=True)
class SchemaSource:
    image_path: str
    git_blob: str
    sha256: str


SCHEMA_SOURCES = {
    "v1": SchemaSource(
        "server/swagger.json",
        "b041d5a2850e5e64c49ebc5dce321f151c9bc193",
        "c3777a701b3e319e515389dd2de4f3f0826e31a886b00a5957db880ca2f7b478",
    ),
    "v3": SchemaSource(
        "api3/swagger.json",
        "20a8830a2142215cc5b4b57bff03e26aaa64752c",
        "76e53f883a1604a763412b11e6d0ba61b902b8f1a529656d24175790fc59d7f6",
    ),
}

_V1_GETS = {
    "/entries": "/entries",
    "/entries/sgv": "/entries/{spec}",
    "/treatments": "/treatments",
    "/status": "/status",
    "/devicestatus": "/devicestatus/",
    "/profile": "/profile",
}
_COLLECTIONS = {
    "devicestatus": "DeviceStatus",
    "entries": "Entry",
    "food": "Food",
    "profile": "Profile",
    "settings": "Settings",
    "treatments": "Treatment",
}
_PROPERTIES = frozenset(("bgnow", "delta", "direction", "iob", "cob", "ar2", "basal"))
_V2_AUTH_REQUEST = "/api/v2/authorization/request/<subject>"
_V2_AUTH_PERMISSION = "/api/v2/authorization/debug/check/<permission>"
_JSON = "application/json"
_FIND = re.compile(
    r"find\[([a-zA-Z_][\w.]*)\](?:\[\$(eq|ne|gt|gte|lt|lte|in|nin|regex|type)\])?(?:\[\d+\])?"
)
_V3_FILTER = re.compile(r"([a-zA-Z_][\w.]*)\$(eq|ne|gt|gte|lt|lte|in|nin|re)")
_DATE_FIELDS = {"date", "created_at", "mills", "srvModified", "srvCreated"}
_NUMBER_FIELDS = {"carbs", "insulin", "duration", "sgv"}
_V3_BARE_ENTRY_FILTERS = {"type", "date"}
_LEGACY_SORT = {"sort[0][0]": "duration", "sort[0][1]": "desc"}
_IDENTIFIER = re.compile(
    r"[0-9a-fA-F]{24}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
)
_UINT = re.compile(r"[0-9]{1,19}")
_MIN_EPOCH_MILLIS = 100_000_000_000
_MAX_EPOCH_MILLIS = 253_402_300_799_999
_MAX_SCHEMA_DIAGNOSTICS = 16
_MAX_INSTANCE_PATH_PARTS = 16


class ContractViolation(ValueError):
    """A failure of schema custody, request admission or native wire invariants."""


@dataclass(frozen=True)
class PublishedSchemaDiagnostic:
    """Pointers and keyword from schema validation without input values."""

    check: str
    instance_pointer: str
    schema_pointer: str
    keyword: str

    def __str__(self):
        return f"{self.check} {self.instance_pointer} ({self.keyword}, schema {self.schema_pointer})"


class PublishedResponseDifference(ValueError):
    """A response from verified stock differs from its published Swagger."""

    def __init__(self, api, path, status, differences):
        self.api = api
        self.path = path
        self.status = status
        self.differences = tuple(differences)
        self.diagnostics = tuple(str(difference) for difference in self.differences)
        super().__init__(
            f"{api} GET {path} HTTP {status}: " + "; ".join(self.diagnostics)
        )


def _pointer(parts):
    return "#" + "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )


class _Schema:
    def __init__(self, api, stock_lib):
        source = SCHEMA_SOURCES[api]
        self.origin = f"{STOCK_COMMIT}:lib/{source.image_path}"
        self.digest = source.sha256
        try:
            raw = (stock_lib / source.image_path).read_bytes()
        except OSError:
            raise ContractViolation(
                f"stock schema unreadable ({self.origin}, expected {self.digest})"
            ) from None
        observed = sha256(raw).hexdigest()
        if observed != self.digest:
            raise ContractViolation(
                f"stock schema digest mismatch ({self.origin}, expected {self.digest}, found {observed})"
            )
        try:
            self.doc = json.loads(raw)
        except (UnicodeError, ValueError):
            raise ContractViolation(
                f"stock schema is invalid JSON ({self.origin}, {self.digest})"
            ) from None
        if (
            not isinstance(self.doc, dict)
            or self.doc.get("openapi") != "3.0.0"
            or self.doc.get("servers") != [{"url": f"/api/{api}"}]
        ):
            raise ContractViolation(
                f"unsupported stock schema format or mount ({self.origin}, {self.digest})"
            )
        self.uri = f"urn:nightscout:15.0.6:{api}"
        self.registry = Registry().with_resource(
            self.uri, Resource(contents=self.doc, specification=DRAFT4)
        )
        self.validators = {}
        self.properties = set()
        self.schema_paths = {}
        self._index(self.doc, ())

    def _index(self, node, path):
        if isinstance(node, dict):
            self.schema_paths[id(node)] = path
            if isinstance(node.get("properties"), dict):
                self.properties.update(node["properties"])
            for key, value in node.items():
                self._index(value, (*path, key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                self._index(value, (*path, index))

    def issues(self, location, instance, label, prefix=()):
        if location not in self.validators:
            schema = self.doc
            try:
                for part in location:
                    schema = schema[part]
            except (KeyError, TypeError, IndexError):
                raise ContractViolation(
                    f"missing published {label} schema ({self.origin}, {self.digest})"
                ) from None
            if not isinstance(schema, dict) or not schema:
                raise ContractViolation(
                    f"invalid published {label} schema ({self.origin}, {self.digest})"
                )
            self.validators[location] = OAS30ReadValidator(
                {"$ref": self.uri + quote(_pointer(location), safe="#/")},
                registry=self.registry,
                format_checker=oas30_format_checker,
            )
        try:
            details = {}
            for error in self.validators[location].iter_errors(instance):
                if len(details) == _MAX_SCHEMA_DIAGNOSTICS:
                    continue
                instance_path = (*prefix, *self._safe_parts(error.absolute_path))
                schema_path = self.schema_paths.get(id(error.schema), location)
                if isinstance(error.schema, dict) and error.validator in error.schema:
                    schema_path = (*schema_path, error.validator)
                diagnostic = PublishedSchemaDiagnostic(
                    label,
                    _pointer(instance_path),
                    _pointer(schema_path),
                    error.validator or "schema",
                )
                details[diagnostic] = None
            return list(details)
        except Unresolvable:
            raise ContractViolation(
                f"unresolved published {label} schema ({self.origin}, {self.digest})"
            ) from None

    def _safe_parts(self, parts):
        sanitized = [
            part if isinstance(part, int) or part in self.properties else "<field>"
            for part in parts
        ]
        if len(sanitized) > _MAX_INSTANCE_PATH_PARTS:
            return [*sanitized[:_MAX_INSTANCE_PATH_PARTS], "<truncated>"]
        return sanitized


@dataclass(frozen=True)
class _Route:
    api: str
    template: str
    path_params: tuple = ()
    component: str | None = None
    properties: frozenset = frozenset()


def _route(path):
    # Accept only the .json aliases observed through the legacy format middleware.
    if (
        not isinstance(path, str)
        or not path.startswith("/api/")
        or "?" in path
        or "#" in path
    ):
        raise ContractViolation("unsupported path")
    api, _, suffix = path[5:].partition("/")
    suffix = "/" + suffix
    if api in ("v1", "v2"):
        legacy = suffix.removesuffix("/").removesuffix(".json")
        if legacy in _V1_GETS:
            spec = (("spec", "sgv"),) if legacy == "/entries/sgv" else ()
            return _Route("v1", _V1_GETS[legacy], spec)
    if api == "v2":
        if suffix == "/properties":
            return _Route("v2", "properties")
        name = suffix.removeprefix("/properties/")
        fields = name.split(",")
        if (
            name != suffix
            and len(fields) == len(set(fields))
            and set(fields) <= _PROPERTIES
        ):
            return _Route("v2", "properties", properties=frozenset(fields))
        if path in (_V2_AUTH_REQUEST, _V2_AUTH_PERMISSION):
            return _Route("v2", path)
    if api == "v3":
        if suffix in ("/version", "/status", "/lastModified"):
            return _Route("v3", suffix)
        collection, _, tail = suffix[1:].partition("/")
        if collection in _COLLECTIONS:
            component = _COLLECTIONS[collection]
            collection_param = (("collection", collection),)
            if suffix == f"/{collection}":
                return _Route(
                    "v3", "/{collection}", collection_param, component + "Array"
                )
            if tail == "history":
                return _Route(
                    "v3", "/{collection}/history", collection_param, component + "Array"
                )
            cursor = tail.removeprefix("history/")
            if (
                cursor != tail
                and _UINT.fullmatch(cursor)
                and _epoch_millis(int(cursor))
            ):
                parameters = (*collection_param, ("lastModified", int(cursor)))
                return _Route(
                    "v3",
                    "/{collection}/history/{lastModified}",
                    parameters,
                    component + "Array",
                )
            if _IDENTIFIER.fullmatch(tail):
                parameters = (*collection_param, ("identifier", tail))
                return _Route("v3", "/{collection}/{identifier}", parameters, component)
    raise ContractViolation("unsupported path")


def _is_number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _epoch_millis(value):
    return type(value) is int and _MIN_EPOCH_MILLIS <= value <= _MAX_EPOCH_MILLIS


def _iso_datetime(value, *, utc=False):
    if not isinstance(value, str) or not re.match(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value
    ):
        return False
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return False
    return not utc or (dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0)


def _v3_time_query(value):
    if isinstance(value, str) and _UINT.fullmatch(value):
        return len(value) == 10 or _epoch_millis(int(value))
    return _iso_datetime(value)


def _numeric_query(value):
    try:
        return not isinstance(value, bool) and math.isfinite(float(value))
    except (ValueError, TypeError, OverflowError):
        return False


def _query_value(value, name, schema):
    if name == "count" or schema.get("type") == "integer":
        if type(value) is int:
            number = value
        elif isinstance(value, str) and _UINT.fullmatch(value):
            number = int(value)
        else:
            raise ContractViolation(f"{name}: expected an integer query value")
        if name == "count" and number < 1:
            raise ContractViolation("count: expected a positive record limit")
        return number
    return value


def _check_find(name, value):
    matched = _FIND.fullmatch(name)
    if not matched:
        raise ContractViolation("unsupported query parameter")
    field, operator = matched.groups()
    if field == "created_at" and not _iso_datetime(value, utc=True):
        raise ContractViolation("find[created_at]: expected UTC ISO date-time")
    if field in (_DATE_FIELDS - {"created_at"}) and (
        not isinstance(value, str)
        or not _UINT.fullmatch(value)
        or not _epoch_millis(int(value))
    ):
        raise ContractViolation("find time operand: expected epoch milliseconds")
    if operator == "type" and (field != "duration" or value != "number"):
        raise ContractViolation("find type operand: unsupported field or type")
    if field in _NUMBER_FIELDS and operator != "type" and not _numeric_query(value):
        raise ContractViolation("find numeric operand: expected a finite numeric value")


def _check_filter(name, value):
    # Stock's search input treats a bare field name as equality. PR 2 and PR 4
    # use that form for entry type and tied timestamps.
    field, operator = (
        (name, "eq")
        if name in _V3_BARE_ENTRY_FILTERS
        else _V3_FILTER.fullmatch(name).groups()
    )
    if not isinstance(value, str) or not value:
        raise ContractViolation("filter operand: expected a non-empty string")
    values = value.split("|") if operator in ("in", "nin") else (value,)
    if any(not item for item in values):
        raise ContractViolation(
            "filter set operand: expected non-empty pipe-separated values"
        )
    if field in _DATE_FIELDS and any(not _v3_time_query(item) for item in values):
        raise ContractViolation(
            "filter date operand: expected ISO date-time, epoch seconds or epoch milliseconds"
        )
    if field in _NUMBER_FIELDS and any(not _numeric_query(item) for item in values):
        raise ContractViolation(
            "filter numeric operand: expected a finite numeric value"
        )


def _parameters(schema, route, location, operation, params, headers):
    definitions = operation.get("parameters", [])
    declarations = {
        (item["in"], item["name"]): (index, item)
        for index, item in enumerate(definitions)
    }
    path_params = dict(route.path_params)
    issues = []

    def check(kind, name, value):
        index, parameter = declarations[(kind, name)]
        candidate = (
            _query_value(value, name, parameter["schema"]) if kind == "query" else value
        )
        issues.extend(
            str(issue)
            for issue in schema.issues(
                (*location, "parameters", index, "schema"), candidate, name
            )
        )

    for (kind, name), (_, parameter) in declarations.items():
        supplied = (
            path_params if kind == "path" else params if kind == "query" else headers
        )
        key = name.lower() if kind == "header" else name
        if parameter.get("required") and key not in supplied:
            issues.append(f"{name}: missing required {kind} parameter")
        if kind == "header" and key in supplied:
            value = headers[key]
            check(kind, name, value)
            if name in ("Last-Modified", "If-Modified-Since"):
                try:
                    parsed = parsedate_to_datetime(value)
                except (TypeError, ValueError, IndexError):
                    parsed = None
                if parsed is None or parsed.tzinfo is None:
                    issues.append(f"{name}: expected an HTTP date with timezone")
    declared_headers = {name.lower() for kind, name in declarations if kind == "header"}
    if set(headers) - declared_headers:
        issues.append("unsupported condition header")
    for name, value in path_params.items():
        if ("path", name) not in declarations:
            issues.append(f"{name}: missing published path parameter")
        else:
            check("path", name, value)
    if "sort" in params and "sort$desc" in params:
        issues.append("sort and sort$desc cannot be combined")
    if set(params) & _LEGACY_SORT.keys() and (
        route.api != "v1"
        or route.template != "/treatments"
        or any(params.get(key) != value for key, value in _LEGACY_SORT.items())
    ):
        issues.append("unsupported legacy duration sort")

    for name, value in params.items():
        if route.api == "v1" and name == "token":
            # Stock authorization accepts this query credential independently
            # of the secret header. The caller can send only a presence marker.
            continue
        if ("query", name) in declarations and name not in (
            "find",
            "filter_parameters",
        ):
            check("query", name, value)
        elif (
            route.api == "v1"
            and ("query", "find") in declarations
            and name.startswith("find[")
        ):
            _check_find(name, value)
            check("query", "find", value)
        elif route.api == "v1" and name in _LEGACY_SORT:
            continue
        elif ("query", "filter_parameters") in declarations and (
            _V3_FILTER.fullmatch(name)
            or (
                path_params.get("collection") == "entries"
                and name in _V3_BARE_ENTRY_FILTERS
            )
        ):
            _check_filter(name, value)
            check("query", "filter_parameters", value)
        else:
            issues.append("unsupported query parameter")
    return issues


def _rows(body, label):
    if not isinstance(body, list) or any(not isinstance(row, dict) for row in body):
        raise ContractViolation(f"{label}: expected an array of objects")
    return body


def _object(value, label):
    if not isinstance(value, dict):
        raise ContractViolation(f"{label}: expected an object")
    return value


def _optional(record, key, predicate, label):
    if key in record and not predicate(record[key]):
        raise ContractViolation(f"{label}.{key}: invalid consumed field")


def _string(value):
    return isinstance(value, str)


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _check_clocks(record, millisecond_keys, iso_keys, label):
    for key in millisecond_keys:
        if key in record and not _epoch_millis(record[key]):
            raise ContractViolation(f"{label}.{key}: expected epoch milliseconds")
    for key in iso_keys:
        if key in record and not _iso_datetime(record[key]):
            raise ContractViolation(f"{label}.{key}: expected ISO date-time")


def _status_settings(body):
    settings = _object(_object(body, "v1 status").get("settings"), "v1 status.settings")
    for key in ("customTitle", "units"):
        _optional(settings, key, _string, "v1 status.settings")
    # Minute settings can be numbers (defaults) or text (environment overrides).
    # Preserve the status payload; stock timeago interprets the minutes later.
    for key in ("alarmTimeagoWarn", "alarmTimeagoUrgent"):
        _optional(
            settings, key, lambda value: type(value) is bool, "v1 status.settings"
        )
    if "thresholds" in settings:
        thresholds = _object(settings["thresholds"], "v1 status.settings.thresholds")
        for key in ("bgLow", "bgTargetBottom", "bgTargetTop", "bgHigh"):
            _optional(thresholds, key, _is_number, "v1 status.settings.thresholds")


def _entry_fields(row, label):
    for key in ("_id", "identifier"):
        _optional(row, key, _string, label)
    _optional(row, "type", _nonempty_string, label)
    _optional(row, "direction", _string, label)
    _optional(row, "units", _string, label)
    _optional(row, "isValid", lambda value: type(value) is bool, label)
    if row.get("type") == "sgv":
        if not _is_number(row.get("sgv")):
            raise ContractViolation(f"{label}.sgv: expected a finite number")
        if not (_epoch_millis(row.get("date")) or _iso_datetime(row.get("dateString"))):
            raise ContractViolation(
                f"{label}.date: expected epoch milliseconds or ISO dateString"
            )


def _treatment_fields(row, label, *, v3=False):
    if not (
        _iso_datetime(row.get("created_at"))
        or _epoch_millis(row.get("mills"))
        or _epoch_millis(row.get("date"))
    ):
        raise ContractViolation(
            f"{label}: expected an occurrence timestamp in created_at, mills or date"
        )
    for key in ("_id", "identifier", "eventType", "notes", "enteredBy"):
        _optional(row, key, _string, label)
    # Stock v1 serveTreatments applies Number() to exactly these two fields.
    # Its JSON serialization can yield null. Stock v3 generic search resolves
    # dates but passes through stored quantities. The published schema still
    # checks every field, and the stock comparison observes each original value.
    if not v3:
        for key in ("carbs", "insulin"):
            if row.get(key) is not None:
                _optional(row, key, _is_number, label)


def _device_fields(row, label):
    for key in ("_id", "identifier"):
        _optional(row, key, _string, label)
    _optional(row, "device", _string, label)
    if "pump" in row:
        pump = _object(row["pump"], f"{label}.pump")
        _optional(pump, "reservoir", _is_number, f"{label}.pump")
        if "status" in pump:
            status = _object(pump["status"], f"{label}.pump.status")
            _check_clocks(status, (), ("timestamp",), f"{label}.pump.status")


def _collection_fields(collection, rows, *, v3=False):
    for index, row in enumerate(rows):
        label = f"{collection}[{index}]"
        if collection in ("entries", "treatments", "devicestatus"):
            _check_clocks(
                row,
                ("date", "mills", "srvModified", "srvCreated"),
                ("created_at",),
                label,
            )
        if collection == "entries":
            _entry_fields(row, label)
        elif collection == "treatments":
            _treatment_fields(row, label, v3=v3)
        elif collection == "devicestatus":
            _device_fields(row, label)


def _check_response_fields(route, body, status):
    if route.api == "v1":
        if route.template == "/status":
            _status_settings(body)
        else:
            collection = route.template.split("/")[1]
            _collection_fields(collection, _rows(body, f"v1 {collection}"))
        return

    envelope = _object(body, "v3 response")
    if type(envelope.get("status")) is not int or envelope["status"] != status:
        raise ContractViolation("v3 body status must match HTTP status")
    if status != 200:
        return
    result = envelope.get("result")
    if route.component:
        rows = (
            _rows(result, "v3 collection result")
            if route.component.endswith("Array")
            else [_object(result, "v3 collection result")]
        )
        _collection_fields(dict(route.path_params)["collection"], rows, v3=True)
    else:
        result = _object(result, "v3 response.result")
        if route.template in ("/version", "/status"):
            if not _nonempty_string(result.get("apiVersion")) or not result[
                "apiVersion"
            ].startswith("3."):
                raise ContractViolation("v3 result.apiVersion: expected a v3 version")
            if not _nonempty_string(result.get("version")):
                raise ContractViolation("v3 result.version: invalid consumed field")
            _check_clocks(result, ("srvDate",), (), "v3 result")
        if route.template == "/status":
            permissions = _object(
                result.get("apiPermissions"), "v3 status.result.apiPermissions"
            )
            for collection in _COLLECTIONS:
                _optional(
                    permissions, collection, _string, "v3 status.result.apiPermissions"
                )
        if route.template == "/lastModified":
            _check_clocks(result, ("srvDate",), (), "v3 lastModified.result")
            clocks = _object(
                result.get("collections"), "v3 lastModified.result.collections"
            )
            _check_clocks(
                clocks, _COLLECTIONS, (), "v3 lastModified.result.collections"
            )


def _v2_properties(body, selected):
    record = _object(body, "v2 properties")
    if selected and set(record) - selected:
        raise ContractViolation(
            "v2 properties response contains an unrequested property"
        )
    for plugin, field in (
        ("bgnow", "last"),
        ("delta", "mgdl"),
        ("iob", "iob"),
        ("cob", "cob"),
    ):
        if plugin in record:
            prop = _object(record[plugin], f"v2 properties.{plugin}")
            _optional(prop, field, _is_number, f"v2 properties.{plugin}")
            if plugin == "bgnow":
                _check_clocks(prop, ("mills",), (), "v2 properties.bgnow")
                if ("last" in prop) != ("mills" in prop):
                    raise ContractViolation(
                        "v2 properties.bgnow: last and mills must refer to the same observation"
                    )
            elif plugin in ("iob", "cob"):
                _optional(prop, "source", _string, f"v2 properties.{plugin}")
    if "direction" in record:
        prop = _object(record["direction"], "v2 properties.direction")
        _optional(prop, "value", _string, "v2 properties.direction")
    if "basal" in record:
        prop = _object(record["basal"], "v2 properties.basal")
        if "current" in prop and prop["current"] is not None:
            current = _object(prop["current"], "v2 properties.basal.current")
            _optional(current, "totalbasal", _is_number, "v2 properties.basal.current")
    if "ar2" in record:
        prop = _object(record["ar2"], "v2 properties.ar2")
        if "forecast" in prop and prop["forecast"] is not None:
            forecast = _object(prop["forecast"], "v2 properties.ar2.forecast")
            if "predicted" in forecast:
                label = "v2 properties.ar2.forecast.predicted"
                for point in _rows(forecast["predicted"], label):
                    if not _epoch_millis(point.get("mills")):
                        raise ContractViolation(
                            f"{label}.mills: expected epoch milliseconds"
                        )
                    if not _is_number(point.get("mgdl")):
                        raise ContractViolation(
                            f"{label}.mgdl: expected a finite number"
                        )


def _v2_response(route, params, status, body):
    allowed_params = (
        {"token", "pretty"} if route.template == "properties" else {"token"}
    )
    if set(params) - allowed_params:
        raise ContractViolation("unsupported query parameter")
    if "pretty" in params and not _nonempty_string(params["pretty"]):
        raise ContractViolation("pretty: expected a non-empty query string")
    record = _object(body, "v2 source response")
    if status == 401:
        if type(record.get("status")) is not int or record["status"] != status:
            raise ContractViolation("v2 body status must match HTTP status")
        if not _nonempty_string(record.get("message")):
            raise ContractViolation("v2 source error requires a message")
        return
    if status != 200:
        raise ContractViolation("unsupported status for v2 source route")
    if route.template == "properties":
        _v2_properties(record, route.properties)
    elif route.template == _V2_AUTH_PERMISSION:
        if record.get("check") is not True:
            raise ContractViolation("unexpected v2 permission response")
    else:
        issued, expires = record.get("iat"), record.get("exp")
        if (
            not _nonempty_string(record.get("token"))
            or not _is_number(issued)
            or not _is_number(expires)
            or not (1_000_000_000 <= issued < expires < 100_000_000_000)
        ):
            raise ContractViolation(
                "unexpected v2 authorization exchange shape or JWT epoch-second clocks"
            )


class StockContract:
    """A validator backed by both stock files and bounded v2 source contracts."""

    @classmethod
    def from_stock_lib(cls, stock_lib):
        """Load stock lib/ copied from the already-running digest-pinned image."""
        root = Path(stock_lib)
        return cls({api: _Schema(api, root) for api in SCHEMA_SOURCES})

    def __init__(self, schemas):
        self._schemas = schemas
        self._served_v1_verified = False

    def verify_served_v1(self, body):
        """Check the actual GET /swagger.json against the copied v1 image file."""
        schema = self._schemas["v1"]
        self._served_v1_verified = False
        if not isinstance(body, dict) or json.dumps(body, sort_keys=True) != json.dumps(
            schema.doc, sort_keys=True
        ):
            raise ContractViolation(
                f"served /swagger.json differs from stock file ({schema.origin}, {schema.digest})"
            )
        self._served_v1_verified = True

    def validate_exchange(
        self,
        method: str,
        path: str,
        *,
        status: int,
        content_type: str,
        body: object,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """Check a verified stock exchange against native and published contracts.

        PublishedResponseDifference reports a Swagger-only response discrepancy
        after native admission and field checks succeed. ContractViolation
        reports a failed custody, request or native response check.

        Dynamic authorization path components must be replaced by the literal
        <subject> or <permission> before passing them here. Authorization
        query values may be replaced with a presence marker. Pass only the
        documented condition headers, without authorization headers.
        """
        if not self._served_v1_verified:
            schema = self._schemas["v1"]
            raise ContractViolation(
                f"unverified served /swagger.json ({schema.origin}, {schema.digest})"
            )
        if method != "GET":
            raise ContractViolation("unsupported method")
        route = _route(path)
        if (
            not isinstance(content_type, str)
            or content_type.partition(";")[0].strip().lower() != _JSON
        ):
            raise ContractViolation("unsupported media type")
        params = {} if params is None else params
        if not isinstance(params, Mapping) or any(
            not isinstance(key, str) for key in params
        ):
            raise ContractViolation("invalid query parameters")
        condition_headers = {}
        if headers is not None:
            if not isinstance(headers, Mapping):
                raise ContractViolation("invalid request headers")
            for name, value in headers.items():
                if isinstance(name, str) and name.lower() in (
                    "if-modified-since",
                    "last-modified",
                ):
                    condition_headers[name.lower()] = value
        if type(status) is not int:
            raise ContractViolation("unsupported status")
        if route.api == "v2":
            if condition_headers:
                raise ContractViolation("unsupported condition header")
            _v2_response(route, params, status, body)
            return "source-v2"

        schema = self._schemas[route.api]
        location = ("paths", route.template, "get")
        operation = schema.doc.get("paths", {}).get(route.template, {}).get("get")
        if not isinstance(operation, dict):
            raise ContractViolation(
                f"missing published GET operation ({schema.origin}, {schema.digest})"
            )
        # Do not silently treat a v1 default as evidence for an observed
        # response status. V3 statuses must also have a published JSON schema.
        response = operation.get("responses", {}).get(str(status), {})
        if _JSON not in response.get("content", {}):
            raise ContractViolation(
                f"unsupported status {status} for GET {route.template}"
            )
        issues = _parameters(
            schema, route, location, operation, params, condition_headers
        )
        if issues:
            raise ContractViolation("; ".join(issues))

        _check_response_fields(route, body, status)
        issues = schema.issues(
            (*location, "responses", str(status), "content", _JSON, "schema"),
            body,
            "published response",
        )
        if (
            status == 200
            and route.component
            and isinstance(body, dict)
            and "result" in body
        ):
            issues.extend(
                schema.issues(
                    ("components", "schemas", route.component),
                    body["result"],
                    "collection response",
                    ("result",),
                )
            )
        if issues:
            raise PublishedResponseDifference(route.api, route.template, status, issues)
        return f"swagger-{route.api}"
