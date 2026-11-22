"""Offline controls for the validator used by the live stock parity suite."""

from dataclasses import replace
import hashlib
import json

import pytest

from tests_parity.stock_contract import (
    ContractViolation,
    PublishedResponseDifference,
    SCHEMA_SOURCES,
    StockContract,
)


def _response(schema):
    return {"content": {"application/json": {"schema": schema}}}


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


@pytest.fixture
def contract(tmp_path, monkeypatch):
    """Small excerpts with the same path/parameter/response binding shape as stock."""
    status = {"properties": {"settings": _ref("Settings")}}
    settings = {"type": "object", "properties": {"timeFormat": {"type": "string"}}}
    treatment = {
        "type": "object",
        "properties": {
            "created_at": {"type": "string"},
            "carbs": {"type": "number"},
            "insulin": {"type": "number"},
            "duration": {"type": "number"},
            "extensions": {
                "type": "object",
                "additionalProperties": {"type": "number"},
            },
        },
    }
    v1 = {
        "paths": {
            "/status": {"get": {"responses": {"200": _response(status)}}},
            "/treatments": {
                "get": {
                    "parameters": [
                        {"name": "find", "in": "query", "schema": {"type": "string"}},
                        {"name": "count", "in": "query", "schema": {"type": "number"}},
                    ],
                    "responses": {
                        "200": _response({"type": "array", "items": _ref("Treatment")}),
                        "default": _response({"type": "object"}),
                    },
                }
            },
        },
        "components": {"schemas": {"Settings": settings, "Treatment": treatment}},
    }
    entry = {
        "type": "object",
        "required": ["app"],
        "properties": {"app": {"type": "string"}, "sgv": {"type": "number"}},
    }
    v3 = {
        "paths": {
            "/version": {
                "get": {"responses": {"200": _response(_ref("Version"))}},
            },
            "/{collection}": {
                "get": {
                    "parameters": [
                        {
                            "name": "collection",
                            "in": "path",
                            "required": True,
                            "schema": {"enum": ["entries", "treatments"]},
                        },
                        {
                            "name": "filter_parameters",
                            "in": "query",
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {"type": "integer", "minimum": 1},
                        },
                    ],
                    "responses": {
                        "200": _response(
                            {
                                "type": "object",
                                "properties": {"result": {"type": "object"}},
                            }
                        ),
                        "401": _response(
                            {
                                "type": "object",
                                "properties": {"status": {"type": "integer"}},
                            }
                        ),
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "EntryArray": {"type": "array", "items": entry},
                "TreatmentArray": {"type": "array", "items": treatment},
                "Version": {
                    "type": "object",
                    "properties": {"result": _ref("VersionResult")},
                },
                "VersionResult": {
                    "type": "object",
                    "properties": {
                        "apiVersion": {"type": "string"},
                        "version": {"type": "string"},
                        "documentedOnly": {"type": "string"},
                    },
                },
            },
        },
    }
    for api, doc in (("v1", v1), ("v3", v3)):
        doc.update(openapi="3.0.0", servers=[{"url": f"/api/{api}"}])
        pin = SCHEMA_SOURCES[api]
        original = json.dumps(doc).encode()
        path = tmp_path / pin.image_path
        path.parent.mkdir(parents=True)
        path.write_bytes(original)
        monkeypatch.setitem(
            SCHEMA_SOURCES,
            api,
            replace(pin, sha256=hashlib.sha256(original).hexdigest()),
        )
    gate = StockContract.from_stock_lib(tmp_path)
    gate.verify_served_v1(v1)
    return gate, v1


def check(
    contract,
    path,
    body,
    *,
    method="GET",
    status=200,
    content_type="application/json",
    params=None,
):
    return contract.validate_exchange(
        method, path, status=status, content_type=content_type, body=body, params=params
    )


def test_rejects_schema_drift_from_image(tmp_path):
    schema = tmp_path / SCHEMA_SOURCES["v1"].image_path
    schema.parent.mkdir(parents=True)
    schema.write_bytes(b"{}")
    with pytest.raises(
        ContractViolation, match="schema digest mismatch.*lib/server/swagger.json"
    ):
        StockContract.from_stock_lib(tmp_path)


def test_requires_live_served_v1_to_match_copied_schema(contract):
    gate, served = contract
    gate.verify_served_v1(served)
    served["paths"].pop("/status")
    with pytest.raises(ContractViolation, match="served.*swagger.json"):
        gate.verify_served_v1(served)
    with pytest.raises(ContractViolation, match="unverified served"):
        check(gate, "/api/v1/treatments.json", [])


def test_legacy_route_alias_binds_operation_and_original_response_schema(contract):
    gate, _ = contract
    path = "/api/v2/treatments.json"
    query = {"find[created_at][$gte]": "2026-11-22T12:35:00.000Z", "count": "10"}
    row = {"created_at": "2026-11-22T12:35:00.000Z", "carbs": 0}
    assert check(gate, path, [row], params=query) == "swagger-v1"
    with pytest.raises(
        PublishedResponseDifference, match=r"#/0/carbs.*type"
    ) as observed:
        check(gate, path, [{**row, "carbs": None}], params=query)
    diagnostic = observed.value.differences[0]
    assert observed.value.path == "/treatments"
    assert (
        diagnostic.schema_pointer
        == "#/components/schemas/Treatment/properties/carbs/type"
    )
    assert diagnostic.keyword == "type"
    with pytest.raises(ContractViolation, match=r"find.*UTC ISO"):
        check(
            gate, path, [row], params={**query, "find[created_at][$gte]": "1795350900"}
        )
    with pytest.raises(ContractViolation, match=r"count"):
        check(gate, path, [row], params={**query, "count": "10leftovers"})


def test_status_requires_schema_fields_and_consumed_object(contract):
    gate, _ = contract
    with pytest.raises(PublishedResponseDifference, match=r"timeFormat.*type"):
        check(gate, "/api/v1/status.json", {"settings": {"timeFormat": 12}})
    with pytest.raises(ContractViolation, match=r"settings.*object"):
        check(gate, "/api/v1/status.json", {})
    with pytest.raises(
        ContractViolation, match=r"thresholds.bgTargetTop.*invalid consumed field"
    ):
        check(
            gate,
            "/api/v1/status.json",
            {
                "settings": {"timeFormat": 12, "thresholds": {"bgTargetTop": "180"}},
            },
        )


def test_schema_evidence_does_not_hide_wrong_legacy_treatment_value(contract):
    gate, _ = contract
    stock_rows = [
        {"created_at": "2026-11-22T12:35:00.000Z", "insulin": 1.5, "carbs": None}
    ]
    path = "/api/v1/treatments.json"
    differences = []

    def observe_and_compare(value):
        try:
            check(gate, path, stock_rows)
        except PublishedResponseDifference as observed:
            differences.extend(observed.diagnostics)
        assert value == stock_rows[0]["insulin"], "legacy insulin differs from stock"

    observe_and_compare(1.5)
    assert differences and all("carbs" in item for item in differences)
    with pytest.raises(AssertionError, match="legacy insulin differs from stock"):
        observe_and_compare(1.0)
    with pytest.raises(ContractViolation, match=r"insulin.*invalid consumed field"):
        check(gate, path, [{**stock_rows[0], "insulin": "wrong"}])


@pytest.mark.parametrize("field", ["carbs", "insulin"])
@pytest.mark.parametrize("stored", ["0", "invalid", True, [6.5]])
def test_native_treatment_quantities_follow_the_endpoint(contract, field, stored):
    gate, _ = contract
    row = {"created_at": "2026-11-22T12:35:00.000Z", field: stored}
    envelope = {"status": 200, "result": [row]}
    with pytest.raises(PublishedResponseDifference, match=rf"{field}.*type"):
        check(gate, "/api/v3/treatments", envelope)
    assert row[field] is stored
    with pytest.raises(ContractViolation, match=rf"{field}.*invalid consumed field"):
        check(gate, "/api/v1/treatments.json", [row])


def test_legacy_duration_is_not_one_of_the_two_coerced_fields(contract):
    gate, _ = contract
    row = {"created_at": "2026-11-22T12:35:00.000Z", "duration": "30"}
    with pytest.raises(PublishedResponseDifference, match=r"duration.*type"):
        check(gate, "/api/v1/treatments.json", [row])
    assert row["duration"] == "30"


def test_published_diagnostics_omit_dynamic_property_names_and_values(contract):
    gate, _ = contract
    stock_rows = [
        {
            "created_at": "2026-11-22T12:35:00.000Z",
            "extensions": {"private-field-marker": "private-value-marker"},
        }
    ]
    with pytest.raises(PublishedResponseDifference) as observed:
        check(gate, "/api/v1/treatments.json", stock_rows)
    assert observed.value.differences[0].instance_pointer == "#/0/extensions/<field>"
    assert "private-field-marker" not in str(observed.value)
    assert "private-value-marker" not in str(observed.value)


@pytest.mark.parametrize(
    ("path", "kwargs", "reason"),
    [
        ("/api/v1/typo", {}, "unsupported path"),
        ("/api/v1/treatments.json", {"method": "POST"}, "unsupported method"),
        ("/api/v1/treatments.json", {"status": 500}, "unsupported status"),
        (
            "/api/v1/treatments.json",
            {"content_type": "text/html"},
            "unsupported media type",
        ),
        (
            "/api/v1/treatments.json",
            {"params": {"unknown": "x"}},
            "unsupported query parameter",
        ),
    ],
)
def test_unsupported_exchange_fails_closed(contract, path, kwargs, reason):
    gate, _ = contract
    with pytest.raises(ContractViolation, match=reason):
        check(gate, path, [], **kwargs)


def test_v3_operation_and_collection_checks_both_run(contract):
    gate, _ = contract
    path = "/api/v3/entries"
    row = {
        "app": "synthetic",
        "identifier": "fixture",
        "type": "sgv",
        "date": 1795350900000,
        "sgv": 132,
    }
    with pytest.raises(
        PublishedResponseDifference, match=r"published response #/result.*type"
    ):
        check(gate, path, {"status": 200, "result": [row]}, params={"limit": 1})
    without_app = {key: value for key, value in row.items() if key != "app"}
    with pytest.raises(
        PublishedResponseDifference, match=r"collection response #/result/0.*required"
    ):
        check(gate, path, {"status": 200, "result": [without_app]})
    with pytest.raises(ContractViolation, match=r"sgv.*finite number"):
        check(gate, path, {"status": 200, "result": [{**row, "sgv": "132"}]})
    with pytest.raises(ContractViolation, match=r"v3 collection result.*array"):
        check(gate, path, {"status": 200, "result": {}})
    with pytest.raises(ContractViolation, match=r"date.*epoch milliseconds"):
        check(gate, path, {"status": 200, "result": [{**row, "date": 1795350900}]})
    with pytest.raises(ContractViolation, match=r"limit.*minimum"):
        check(gate, path, {"status": 200, "result": []}, params={"limit": 0})
    with pytest.raises(ContractViolation, match=r"filter date operand"):
        check(gate, path, {"status": 200, "result": []}, params={"date$lte": "139"})


def test_v3_version_validates_nested_ref_and_consumer_fields(contract):
    gate, _ = contract
    path = "/api/v3/version"
    payload = {
        "status": 200,
        "result": {"apiVersion": "3.0.3-alpha", "version": "15.0.6"},
    }
    assert check(gate, path, payload) == "swagger-v3"
    del payload["result"]["apiVersion"]
    with pytest.raises(ContractViolation, match=r"apiVersion.*expected a v3 version"):
        check(gate, path, payload)
    payload["result"]["version"] = 15.06
    payload["result"]["apiVersion"] = "3.0.3-alpha"
    with pytest.raises(
        ContractViolation, match=r"v3 result.version.+invalid consumed field"
    ):
        check(gate, path, payload)
    payload["result"]["version"] = "15.0.6"
    payload["result"]["documentedOnly"] = 12
    with pytest.raises(
        PublishedResponseDifference,
        match=r"published response #/result/documentedOnly.*type",
    ):
        check(gate, path, payload)


def test_v3_uses_declared_status_and_enforces_error_envelope(contract):
    gate, _ = contract
    path = "/api/v3/entries"
    assert (
        check(gate, path, {"status": 401, "message": "denied"}, status=401)
        == "swagger-v3"
    )
    with pytest.raises(ContractViolation, match="body status must match HTTP status"):
        check(gate, path, {"status": 200}, status=401)
    with pytest.raises(ContractViolation, match="unsupported status"):
        check(gate, path, {"status": 404}, status=404)


def test_v2_properties_preserve_absence_and_validate_present_signal_clock(contract):
    gate, _ = contract
    path = "/api/v2/properties/bgnow,cob,ar2"
    body = {
        "bgnow": {"last": 132, "mills": 1795350900000},
        "cob": {"cob": 0},
        "ar2": {"forecast": {"predicted": [{"mills": 1795351802000, "mgdl": 140}]}},
    }
    assert check(gate, path, body) == "source-v2"
    assert check(gate, path, {}) == "source-v2"
    with pytest.raises(
        ContractViolation, match=r"ar2.forecast.predicted.*epoch milliseconds"
    ):
        check(
            gate,
            path,
            {**body, "ar2": {"forecast": {"predicted": [{"mills": 2, "mgdl": 140}]}}},
        )
    with pytest.raises(ContractViolation, match="unrequested property"):
        check(gate, path, {**body, "delta": {"mgdl": 5}})
    with pytest.raises(ContractViolation, match="unsupported path"):
        check(gate, "/api/v2/properties/unknown", {})


def test_source_auth_supports_status_with_sanitized_route_and_payload(contract):
    gate, _ = contract
    path = "/api/v2/authorization/debug/check/<permission>"
    assert check(gate, path, {"check": True}) == "source-v2"
    assert (
        check(gate, path, {"status": 401, "message": "Unauthorized"}, status=401)
        == "source-v2"
    )
    with pytest.raises(ContractViolation, match="permission response"):
        check(gate, path, {"check": False})
    with pytest.raises(ContractViolation) as unsupported:
        check(gate, "/api/v2/authorization/request/example-value", {})
    assert "example-value" not in str(unsupported.value)
