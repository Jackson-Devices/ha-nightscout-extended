"""Offline validation of public Nightscout 15.0.6 JSON responses.

Supply original image files from /opt/app at the repository-relative paths in
SCHEMA_SOURCES. The v1 file is served by /swagger.json and /api-docs. The v3
file is used by /api3-docs. Both are loaded by lib/server/app.js at STOCK_COMMIT.
The v2 API root mounts the v1 router in lib/api2/index.js at the same commit.
"""

from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
from urllib.parse import quote


STOCK_COMMIT = "9cd304f78a5c12401c9711cbd56d2a12eaca0632"
SCHEMA_SOURCES = {
    "v1": {
        "path": "lib/server/swagger.json",
        "git_blob": "b041d5a2850e5e64c49ebc5dce321f151c9bc193",
        "sha256": "c3777a701b3e319e515389dd2de4f3f0826e31a886b00a5957db880ca2f7b478",
    },
    "v3": {
        "path": "lib/api3/swagger.json",
        "git_blob": "20a8830a2142215cc5b4b57bff03e26aaa64752c",
        "sha256": "76e53f883a1604a763412b11e6d0ba61b902b8f1a529656d24175790fc59d7f6",
    },
}
_VALIDATOR_VERSIONS = {
    "openapi-schema-validator": "0.6.3",
    "jsonschema": "4.25.1",
    "referencing": "0.37.0",
}
_JSON = "application/json"
_V1_PATHS = {
    "/status": "/status",
    "/status.json": "/status",
    "/entries": "/entries",
    "/entries.json": "/entries",
    "/entries/sgv": "/entries/{spec}",
    "/entries/sgv.json": "/entries/{spec}",
    "/treatments": "/treatments",
    "/treatments.json": "/treatments",
    "/devicestatus": "/devicestatus/",
    "/profile": "/profile",
}
_V2_PROPERTIES = {"bgnow", "delta", "direction", "iob", "cob", "ar2", "basal"}
_V3_COLLECTIONS = {
    "devicestatus": "DeviceStatus",
    "entries": "Entry",
    "food": "Food",
    "profile": "Profile",
    "settings": "Settings",
    "treatments": "Treatment",
}
_V3_SPECIFIC_PATHS = {"/version", "/status", "/lastModified"}
_CURSOR = re.compile(r"[0-9]{1,19}")
_IDENTIFIER = re.compile(
    r"[0-9a-fA-F]{24}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
)
_JSON_TYPES = {
    type(None): "null", bool: "boolean", int: "integer", float: "number",
    str: "string", list: "array", dict: "object",
}


class SwaggerGateSetupError(RuntimeError):
    """The pinned schema or validator could not be used."""


@dataclass(frozen=True)
class _Route:
    public_path: str
    schema_path: str | None
    collection_schema: str | None = None


def _route_for(api, path):
    mount = f"/api/{api}"
    path = path.partition("?")[0]
    if not path.startswith(mount + "/"):
        return None
    route = path[len(mount):].removesuffix("/")
    if api in ("v1", "v2"):
        if route in _V1_PATHS:
            return _Route(path, _V1_PATHS[route])
        prefix = "/properties/"
        if api == "v2" and route.startswith(prefix):
            fields = set(route[len(prefix):].split(","))
            if fields <= _V2_PROPERTIES:
                return _Route(path, None)
        return None

    if route in _V3_SPECIFIC_PATHS:
        return _Route(path, route)
    collection, _, tail = route[1:].partition("/")
    if collection not in _V3_COLLECTIONS:
        return None
    public_path = f"{mount}/{collection}"
    component = _V3_COLLECTIONS[collection]
    if not tail:
        return _Route(public_path, "/{collection}", component + "Array")
    if tail == "history":
        return _Route(public_path + "/history", "/{collection}/history", component + "Array")
    if tail.startswith("history/") and _CURSOR.fullmatch(tail[len("history/"):]):
        return _Route(public_path + "/history/{lastModified}", "/{collection}/history/{lastModified}",
                      component + "Array")
    if _IDENTIFIER.fullmatch(tail):
        return _Route(public_path + "/{identifier}", "/{collection}/{identifier}", component)
    return None


def _pointer(parts):
    return "#" + "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _load_validator():
    for package, expected in _VALIDATOR_VERSIONS.items():
        try:
            installed = version(package)
        except PackageNotFoundError:
            raise SwaggerGateSetupError(f"validator_dependency_missing:{package}") from None
        if installed != expected:
            raise SwaggerGateSetupError(f"validator_dependency_version:{package}")
    try:
        from openapi_schema_validator import OAS30ReadValidator, oas30_format_checker
        from referencing import Registry, Resource
        from referencing.exceptions import Unresolvable
        from referencing.jsonschema import DRAFT4
    except (ImportError, OSError):
        raise SwaggerGateSetupError("validator_dependency_import_failed") from None
    return OAS30ReadValidator, oas30_format_checker, Registry, Resource, Unresolvable, DRAFT4


class _Schema:
    def __init__(self, api, path, engine):
        validator, format_checker, registry, resource, self.unresolvable, draft = engine
        self.api = api
        self.validator = validator
        self.format_checker = format_checker
        self.uri = f"urn:nightscout:stock-15.0.6:{api}"
        try:
            contents = Path(path).read_bytes()
        except OSError:
            raise SwaggerGateSetupError(f"schema_unreadable:{api}") from None
        if hashlib.sha256(contents).hexdigest() != SCHEMA_SOURCES[api]["sha256"]:
            raise SwaggerGateSetupError(f"schema_hash_mismatch:{api}")
        try:
            self.document = json.loads(contents)
        except (UnicodeError, ValueError):
            raise SwaggerGateSetupError(f"schema_json_invalid:{api}") from None
        if self.document.get("openapi") != "3.0.0":
            raise SwaggerGateSetupError(f"schema_version_unexpected:{api}")
        self.registry = registry().with_resource(self.uri, resource(contents=self.document, specification=draft))
        self.pointers = {}
        self.property_names = set()
        self.validators = {}
        self._index(self.document, ())

    def _index(self, node, path):
        if isinstance(node, dict):
            self.pointers[id(node)] = path
            self.property_names.update(node.get("properties", {}).keys())
            for name, value in node.items():
                self._index(value, (*path, name))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                self._index(value, (*path, index))

    def _validator_for(self, location):
        if location not in self.validators:
            selected = self.document
            try:
                for part in location:
                    selected = selected[part]
            except (KeyError, TypeError):
                raise SwaggerGateSetupError(f"response_schema_missing:{self.api}") from None
            if not isinstance(selected, dict) or not selected:
                raise SwaggerGateSetupError(f"response_schema_invalid:{self.api}")
            # The synthetic reference selects an original Schema Object. The empty
            # registry's retrieval policy cannot fetch remote references.
            uri = self.uri + quote(_pointer(location), safe="#/")
            self.validators[location] = self.validator(
                {"$ref": uri}, registry=self.registry, format_checker=self.format_checker,
            )
        return self.validators[location]

    def diagnostics(self, location, instance, base, validation, instance_prefix=()):
        validator = self._validator_for(location)
        issues = []
        try:
            for error in validator.iter_errors(instance):
                issue = self._describe(error, location, base, validation, instance_prefix)
                # The OAS 3.0 validator may emit two type errors for one null value.
                if issue not in issues:
                    issues.append(issue)
        except self.unresolvable:
            raise SwaggerGateSetupError(f"schema_reference_unresolved:{self.api}") from None
        return issues

    def _describe(self, error, entry, base, validation, instance_prefix):
        instance_path = (*instance_prefix, *error.absolute_path)
        safe_parts = [
            part if isinstance(part, int) or part in self.property_names else "<property>"
            for part in instance_path
        ]
        keyword = error.validator or "schema"
        schema_path = self.pointers.get(id(error.schema), entry)
        issue = {
            **base,
            "classification": "schema-mismatch",
            "validation": validation,
            "instance_pointer": _pointer(safe_parts),
            "schema_pointer": _pointer((*schema_path, keyword)),
            "keyword": keyword,
            "actual_type": _JSON_TYPES.get(type(error.instance), "non-json"),
        }
        if isinstance(error.schema, dict) and "type" in error.schema:
            issue["expected_type"] = error.schema["type"]
        if keyword == "format":
            issue["expected_format"] = error.validator_value
        elif keyword == "oneOf":
            issue["one_of_outcome"] = "zero_matches" if error.context else "multiple_matches"
        elif keyword == "required":
            properties = error.schema.get("properties", {})
            issue["missing_properties"] = [
                name for name in error.validator_value
                if name not in error.instance and not properties.get(name, {}).get("writeOnly")
            ]
        indexes = [part for part in instance_path if isinstance(part, int)]
        if indexes:
            issue["row_index"] = indexes[0]
        return issue


class SwaggerGate:
    """Apply the original 200 JSON response schemas to decoded, unprojected bodies.

    An empty result means the published checks ran without a mismatch. Keep
    source-only observations separate. A schema-mismatch or unverified result
    must fail the strict gate. No request query, body values, or private route
    segments are included in diagnostics.
    """

    def __init__(self, v1_path, v3_path):
        engine = _load_validator()
        self._schemas = {api: _Schema(api, path, engine) for api, path in
                         (("v1", v1_path), ("v3", v3_path))}

    def validate(self, api, path, body, *, method="GET", status=200, content_type=_JSON):
        if api not in ("v1", "v2", "v3"):
            return [{"classification": "unverified", "reason": "api_out_of_scope"}]
        schema_api = "v1" if api == "v2" else api
        base = {"api": api, "schema_api": schema_api}
        if method != "GET":
            return [{**base, "classification": "unverified", "reason": "method_out_of_scope"}]
        route = _route_for(api, path)
        if route is None:
            return [{**base, "classification": "unverified", "reason": "route_out_of_scope"}]
        base["public_path"] = route.public_path
        if type(status) is not int or status != 200:
            if type(status) is int and 100 <= status <= 599:
                base["http_status"] = status
            return [{**base, "classification": "unverified", "reason": "response_status_out_of_scope"}]
        base["http_status"] = status
        if content_type.partition(";")[0].strip().lower() != _JSON:
            return [{**base, "classification": "unverified", "reason": "response_media_out_of_scope"}]
        if route.schema_path is None:
            return [{**base, "classification": "source-only", "reason": "no_published_response_schema"}]

        schema = self._schemas[schema_api]
        response = ("paths", route.schema_path, "get", "responses", "200", "content", _JSON, "schema")
        issues = schema.diagnostics(response, body, base, "published-response")
        if route.collection_schema is not None:
            # This supplemental check never selects or replaces a oneOf branch.
            if not isinstance(body, dict) or "result" not in body:
                issues.append({**base, "classification": "unverified", "reason": "collection_result_missing"})
            else:
                component = ("components", "schemas", route.collection_schema)
                issues.extend(schema.diagnostics(component, body["result"], base, "collection-result", ("result",)))
        return issues
