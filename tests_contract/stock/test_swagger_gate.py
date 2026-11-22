"""Controls using the original swagger.json files from the pinned stock image.

Set NS_STOCK_V1_SWAGGER and NS_STOCK_V3_SWAGGER to the byte-exact copies, then
run this file with the controller Python and the Swagger dependency lock.
"""

import json
import os
from pathlib import Path
import tempfile
import unittest

from swagger_gate import SwaggerGate, SwaggerGateSetupError


class TestStockSwaggerGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v1 = Path(os.environ["NS_STOCK_V1_SWAGGER"])
        cls.v3 = Path(os.environ["NS_STOCK_V3_SWAGGER"])
        cls.gate = SwaggerGate(cls.v1, cls.v3)

    def test_optional_quantity_and_unknown_property(self):
        body = [{"insulin": 0, "insulinUnitsFromFixture": "allowed"}]
        self.assertEqual(self.gate.validate("v1", "/api/v1/treatments.json", body), [])
        self.assertEqual(self.gate.validate("v1", "/api/v1/treatments.json", [{}]), [])

    def test_string_boolean_and_present_null_are_not_numbers(self):
        for value, actual in (("private-fixture-value", "string"), (True, "boolean"), (None, "null")):
            with self.subTest(actual=actual):
                issues = self.gate.validate("v1", "/api/v1/treatments.json?ignored=private-fixture-value",
                                            [{"insulin": value}])
                self.assertEqual(len(issues), 1)
                issue = issues[0]
                self.assertEqual(issue["classification"], "schema-mismatch")
                self.assertEqual(issue["schema_pointer"], "#/components/schemas/Treatment/properties/insulin/type")
                self.assertEqual(issue["expected_type"], "number")
                self.assertEqual(issue["actual_type"], actual)
                self.assertEqual(issue["row_index"], 0)
                self.assertNotIn("private-fixture-value", json.dumps(issue))

    def test_whole_response_and_mounted_v2_alias(self):
        v1 = self.gate.validate("v1", "/api/v1/entries/sgv.json", {"sgv": 100})
        v2 = self.gate.validate("v2", "/api/v2/entries/sgv.json", {"sgv": 100})
        self.assertEqual(len(v1), 1)
        self.assertEqual(len(v2), 1)
        self.assertEqual(v2[0]["api"], "v2")
        self.assertEqual(v2[0]["schema_api"], "v1")
        self.assertEqual(v2[0]["public_path"], "/api/v2/entries/sgv.json")
        self.assertEqual(v2[0]["schema_pointer"], v1[0]["schema_pointer"])
        self.assertEqual(v2[0]["expected_type"], "array")
        self.assertEqual(v2[0]["actual_type"], "object")

    def test_v3_search_and_history_keep_generic_array_assertions(self):
        body = {"status": 200, "result": [{"date": 1_700_000_000_000, "app": "fixture", "sgv": 100}]}
        for path in ("/api/v3/entries", "/api/v3/entries/history", "/api/v3/entries/history/1700000000000"):
            with self.subTest(path=path):
                issues = self.gate.validate("v3", path, body)
                self.assertEqual({x["validation"] for x in issues}, {"published-response"})
                self.assertEqual(
                    {x["schema_pointer"] for x in issues},
                    {"#/components/schemas/DocumentArray/type", "#/components/schemas/DocumentArray/oneOf"},
                )

    def test_invalid_collection_row_does_not_hide_generic_one_of(self):
        body = {"status": 200, "result": [{"date": 1_700_000_000_000, "app": "fixture", "sgv": "private-row"}]}
        issues = self.gate.validate("v3", "/api/v3/entries", body)
        self.assertTrue(any(x["schema_pointer"] == "#/components/schemas/DocumentArray/oneOf" for x in issues))
        typed = [x for x in issues if x["validation"] == "collection-result"]
        self.assertEqual(len(typed), 1)
        self.assertEqual(typed[0]["schema_pointer"], "#/components/schemas/Entry/allOf/1/properties/sgv/type")
        self.assertEqual(typed[0]["instance_pointer"], "#/result/0/sgv")
        self.assertNotIn("private-row", json.dumps(issues))

    def test_literal_singular_document_one_of(self):
        body = {"status": 200, "result": {"date": 1_700_000_000_000, "app": "fixture", "sgv": 100}}
        identifier = "ac8346ef74fdd790596de0ff"
        issues = self.gate.validate("v3", f"/api/v3/entries/{identifier}", body)
        self.assertEqual({x["schema_pointer"] for x in issues}, {"#/components/schemas/Document/oneOf"})
        self.assertEqual(issues[0]["public_path"], "/api/v3/entries/{identifier}")
        self.assertNotIn(identifier, json.dumps(issues))

    def test_scope_and_setup_failures_are_explicit(self):
        props = self.gate.validate("v2", "/api/v2/properties/bgnow,cob", {"bgnow": {}})
        unknown = self.gate.validate("v1", "/api/v1/private-path", None)
        denied = self.gate.validate("v3", "/api/v3/entries", None, status=403)
        self.assertEqual(props[0]["classification"], "source-only")
        self.assertEqual(unknown[0]["classification"], "unverified")
        self.assertEqual(denied[0]["classification"], "unverified")
        self.assertNotIn("private-path", json.dumps(unknown))
        with tempfile.TemporaryDirectory() as scratch:
            altered = Path(scratch) / "swagger.json"
            altered.write_bytes(self.v1.read_bytes() + b" ")
            with self.assertRaisesRegex(SwaggerGateSetupError, "schema_hash_mismatch:v1"):
                SwaggerGate(altered, self.v3)


if __name__ == "__main__":
    unittest.main()
