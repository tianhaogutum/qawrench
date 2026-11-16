"""Explicit request-header rows: case, order, diagnostics and publication guards."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from qawrench.har import (DoctorError, build_plan, build_preflight, load_json,
                               request_key, rewrite_har, validate_rules)
from qawrench.har.fixtures import load_fixture

ROOT = Path(__file__).resolve().parents[2]
RULES = {"dropRequestHeaders": ["X-Request-ID", "Authorization"]}
COUNTS = [{"name": "authorization", "count": 2}, {"name": "x-request-id", "count": 2}]


def fixture():
    return load_json(ROOT / "examples/har/request-headers.har")


def expected(source):
    result = copy.deepcopy(source)
    for entry, indexes in zip(result["log"]["entries"], ([1, 2, 3], [0], [])):
        req = entry["request"]
        req["headers"] = [row for i, row in enumerate(req["headers"]) if i not in indexes]
    return result


class RequestHeaderTests(unittest.TestCase):
    def test_mixed_case_duplicate_rows_removed_without_other_changes(self):
        source = fixture()
        original = copy.deepcopy((source, RULES))
        result, plan = rewrite_har(source, RULES)
        self.assertEqual(result, expected(source))
        self.assertEqual((source, RULES), original)
        self.assertEqual(plan, build_plan(source, RULES))
        self.assertEqual(plan["summary"], {
            "entries": 3, "changedEntries": 2, "collisionGroups": 0, "conflictingGroups": 0,
            "removedRequestHeaderRows": 4, "removedRequestHeaders": COUNTS})
        self.assertEqual(plan["entries"][0]["reasons"], [{
            "kind": "dropRequestHeaders", "removedIndexes": [1, 2, 3],
            "removedHeaders": [{"name": "authorization", "count": 1}, {"name": "x-request-id", "count": 2}]}])
        self.assertEqual(plan["entries"][1]["reasons"], [{
            "kind": "dropRequestHeaders", "removedIndexes": [0],
            "removedHeaders": [{"name": "authorization", "count": 1}]}])
        self.assertEqual(plan["entries"][2]["reasons"], [])
        for row in source["log"]["entries"][0]["request"]["headers"][1:4] + source["log"]["entries"][1]["request"]["headers"][:1]:
            self.assertNotIn(row["value"], json.dumps(plan))
        self.assertEqual([request_key(e["request"]) for e in source["log"]["entries"]],
                         [request_key(e["request"]) for e in result["log"]["entries"]])
        self.assertEqual(base64.b64decode(result["log"]["entries"][1]["request"]["postData"]["text"]), b"\x00\xff\r\n")

    def test_no_matches_empty_omitted_and_repeat_are_noops(self):
        source = fixture()
        for rules in ({}, {"dropRequestHeaders": []}, {"dropRequestHeaders": ["X-Absent"]}):
            with self.subTest(rules=rules):
                result, plan = rewrite_har(source, rules)
                self.assertEqual(result, source)
                self.assertEqual(plan["summary"]["changedEntries"], 0)
                if rules:
                    self.assertEqual(plan["summary"]["removedRequestHeaders"], [])
                    self.assertEqual(plan["summary"]["removedRequestHeaderRows"], 0)
                else:
                    self.assertNotIn("removedRequestHeaders", plan["summary"])
        result, _ = rewrite_har(source, RULES)
        repeated, plan = rewrite_har(result, RULES)
        self.assertEqual(repeated, result)
        self.assertEqual(plan["summary"]["removedRequestHeaderRows"], 0)
        self.assertEqual(plan["summary"]["changedEntries"], 0)
        self.assertEqual(build_plan({**source, "log": {**source["log"], "entries": []}}, RULES)["summary"]["removedRequestHeaders"], [])

    def test_rule_token_grammar_duplicates_and_literal_star(self):
        for invalid in (None, {}, "Authorization", 1, True, [None], [False], [1], [[]], [""],
                        [" Authorization"], ["Authorization "], ["X Y"], ["X:Y"], ["X\nY"],
                        ["X\r"], ["X\tY"], ["X\x00"], ["X\x7f"], ["X-é"], ["X-K"],
                        ["X?"], ["X/"], ["X, Y"], ["X", "x"], ["X", "X"]):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(DoctorError, r"rules.dropRequestHeaders"):
                validate_rules({"dropRequestHeaders": invalid})
        token = "!#$%&'*+.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-"
        validate_rules({"dropRequestHeaders": [token]})
        source = fixture()
        source["log"]["entries"][0]["request"]["headers"].append({"name": "*", "value": "literal"})
        result, plan = rewrite_har(source, {"dropRequestHeaders": ["*"]})
        self.assertEqual(result["log"]["entries"][0]["request"]["headers"], source["log"]["entries"][0]["request"]["headers"][:-1])
        self.assertEqual(plan["summary"]["removedRequestHeaderRows"], 1)
        # Case and rule order cannot change the removal summary/reasons.
        alternate = build_plan(fixture(), {"dropRequestHeaders": ["AUTHORIZATION", "x-request-id"]})
        self.assertEqual(alternate["summary"], build_plan(fixture(), RULES)["summary"])
        self.assertEqual(alternate["entries"], build_plan(fixture(), RULES)["entries"])

    def test_combined_url_rules_keep_conflicts_and_inventory_gates(self):
        source = fixture()
        rules = {**RULES, "origins": {"https://stage.example": "http://fixture.test"},
                 "dropQueryKeys": ["Authorization"]}
        result, plan = rewrite_har(source, rules)
        self.assertEqual(result["log"]["entries"][0]["request"]["url"], "http://fixture.test/a")
        self.assertEqual(result["log"]["entries"][0]["request"]["queryString"], [])
        wanted = {"schemaVersion": 1, "requests": [{"id": "upload", **{
            k: copy.deepcopy(v) for k, v in result["log"]["entries"][0]["request"].items()
            if k in ("url", "method", "postData", "headers")}}]}
        saved = copy.deepcopy(wanted)
        self.assertTrue(build_preflight(source, wanted, rules)["passed"])
        self.assertEqual(rewrite_har(source, rules, inventory=wanted)[0], result)
        self.assertEqual(wanted, saved)
        wanted["requests"][0]["url"] += "/missing"
        with self.assertRaisesRegex(DoctorError, "request preflight failed"):
            rewrite_har(source, rules, inventory=wanted)
        source["log"]["entries"].append(copy.deepcopy(source["log"]["entries"][0]))
        source["log"]["entries"][-1]["response"]["headers"].append({"name": "X-Different", "value": "yes"})
        self.assertFalse(build_plan(source, rules)["safeToApply"])
        with self.assertRaisesRegex(DoctorError, "conflicting rewritten request keys"):
            rewrite_har(source, rules)
        source["log"]["entries"][-1]["response"] = copy.deepcopy(source["log"]["entries"][0]["response"])
        wanted["requests"][0]["url"] = "http://fixture.test/a"
        self.assertTrue(build_plan(source, rules)["safeToApply"])
        with self.assertRaisesRegex(DoctorError, "request preflight failed.*ambiguous"):
            rewrite_har(source, rules, inventory=wanted)

    def test_ascii_matching_does_not_select_non_ascii_lookalikes(self):
        source = fixture()
        source["log"]["entries"] = source["log"]["entries"][:1]
        source["log"]["entries"][0]["request"]["headers"] = [
            {"name": name, "value": "opaque"} for name in ("K", "K", "k", "X-*")]
        result, plan = rewrite_har(source, {"dropRequestHeaders": ["k", "X-*"]})
        self.assertEqual(result["log"]["entries"][0]["request"]["headers"], [{"name": "K", "value": "opaque"}])
        self.assertEqual(plan["summary"]["removedRequestHeaders"], [{"name": "k", "count": 2}, {"name": "x-*", "count": 1}])
        # A selected array can become empty; unrelated request fields survive.
        source["log"]["entries"][0]["request"]["headers"].pop(1)
        result, _ = rewrite_har(source, {"dropRequestHeaders": ["k", "X-*"]})
        self.assertEqual(result["log"]["entries"][0]["request"]["headers"], [])

    def test_original_validation_cannot_be_bypassed_by_header_removal(self):
        for row in ({"name": "Authorization"}, {"name": "Authorization", "value": None}):
            source = fixture()
            source["log"]["entries"][-1]["request"]["headers"].append(row)
            with self.assertRaisesRegex(DoctorError, r"log.entries\[2\].request.headers\[1\].value"):
                rewrite_har(source, RULES)
        source = load_json(ROOT / "examples/har/multipart.har")
        result, _ = rewrite_har(source, {"dropRequestHeaders": ["Content-Type"]})
        self.assertEqual(result["log"]["entries"][0]["request"]["postData"], source["log"]["entries"][0]["request"]["postData"])
        self.assertFalse(any(h["name"].lower() == "content-type" for h in result["log"]["entries"][0]["request"]["headers"]))
        source["log"]["entries"][0]["request"]["headers"].append({"name": "Content-Type", "value": "multipart/form-data; boundary=wrong"})
        with self.assertRaisesRegex(DoctorError, "boundary/media type disagrees"):
            rewrite_har(source, {"dropRequestHeaders": ["Content-Type"]})

    def test_zip_projects_headers_into_original_manifest_and_preserves_attachments(self):
        source = fixture()
        req = source["log"]["entries"][1]["request"]
        req["postData"].pop("encoding")
        req["postData"].update(text="", _file="upload.bin")
        attachments = {"upload.bin": b"\x00\xff\r\n", "unused.bin": b"unused\x00"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "source.har.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("recording.har", json.dumps(source))
                for name, data in attachments.items():
                    archive.writestr(name, data)
            original = path.read_bytes()
            fixture_view = load_fixture(path)
            result, plan = rewrite_har(fixture_view.har, RULES)
            out = root / "out.har.zip"
            fixture_view.write(result, out, protected=[path])
            with zipfile.ZipFile(out) as archive:
                self.assertEqual(json.loads(archive.read("recording.har")), expected(source))
                self.assertEqual(archive.namelist(), ["recording.har", *attachments])
                for name, data in attachments.items():
                    self.assertEqual(archive.read(name), data)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(plan["summary"]["removedRequestHeaders"], COUNTS)


if __name__ == "__main__":
    unittest.main()
