"""Exact-key misses may explain same-URL candidates, never select them."""
import copy
import unittest

from examples.har.fixtures import RULES, entry, har
from qawrench.har import DoctorError, build_plan, build_preflight, rewrite_har
from .test_preflight import inventory, request


def candidate(index, *fields):
    return {"entryIndex": index, "entryId": f"/log/entries/{index}",
            "differingFields": list(fields)}


class NearMatchTests(unittest.TestCase):
    def test_missing_lists_all_same_url_entries_in_har_order_with_exact_differences(self):
        source = har([entry("http://fixture.test/a", "GET", "wanted"),
                      entry("http://fixture.test/a", "POST", "different"),
                      entry("http://fixture.test/a", "GET", "wanted"),
                      entry("http://fixture.test/a", "post", "different"),
                      entry("http://fixture.test/other", "POST", "wanted")])
        wanted = inventory(request("missing", method="POST", postData={
            "mimeType": "text/plain", "text": "wanted"}))
        saved = copy.deepcopy((source, wanted))
        report = build_preflight(source, wanted)
        result = report["requests"][0]
        self.assertEqual(result["nearMatches"], [candidate(0, "method"), candidate(1, "body"),
                                                candidate(2, "method"), candidate(3, "method", "body")])
        self.assertEqual((result["status"], result["entryIndexes"], result["entryIds"]), ("missing", [], []))
        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"], {"requests": 1, "matchedRequests": 0,
                                            "missingRequests": 1, "ambiguousRequests": 0})
        self.assertEqual(report, build_preflight(source, wanted))
        self.assertEqual((source, wanted), saved)

    def test_candidates_use_projected_urls_but_inventory_and_literal_url_spelling_stay_exact(self):
        source = har([entry("https://stage.example/a?cacheBust=1", "POST", "body"),
                      entry("http://fixture.test/a", "GET"),
                      entry("http://fixture.test/a?", "PUT"),
                      entry("http://fixture.test/a?x=a+b", "PUT"),
                      entry("http://fixture.test/a?b=2&a=1", "PUT"),
                      entry("http://FIXTURE.test/a", "PUT"),
                      entry("http://fixture.test/a#fragment", "PUT")])
        wanted = inventory(request("projected", method="PUT"),
                           request("raw", "https://stage.example/a?cacheBust=1", method="PUT"),
                           request("escape", "http://fixture.test/a?x=a%20b"),
                           request("query-order", "http://fixture.test/a?a=1&b=2"))
        saved = copy.deepcopy((source, wanted, RULES))
        report = build_preflight(source, wanted, RULES)
        self.assertEqual([r["nearMatches"] for r in report["requests"]],
                         [[candidate(0, "method", "body"), candidate(1, "method")], [], [], []])
        self.assertEqual(build_preflight(source, wanted)["requests"][1]["nearMatches"],
                         [candidate(0, "method", "body")])
        self.assertEqual((source, wanted, RULES), saved)

    def test_decoded_bytes_control_differences_on_all_methods_including_binary_and_empty(self):
        source = har([entry("http://fixture.test/a", "GET", "é"),
                      entry("http://fixture.test/empty", "GET"),
                      entry("http://fixture.test/binary", "GET", "placeholder")])
        source["log"]["entries"][2]["request"]["postData"] = {
            "mimeType": "application/octet-stream", "encoding": "base64", "text": "AP8="}
        source["log"]["entries"][2]["request"]["bodySize"] = 2
        wanted = inventory(
            request("same-bytes", method="get", headers=[{"name": "X", "value": "ignored"}],
                    postData={"mimeType": "other/type", "encoding": "base64", "text": "w6k="}),
            request("different-spelling", postData={"mimeType": "text/plain", "text": "e\u0301"}),
            request("empty", "http://fixture.test/empty", method="POST",
                    postData={"mimeType": "text/plain", "text": ""}),
            request("binary", "http://fixture.test/binary", method="POST",
                    postData={"mimeType": "application/octet-stream", "encoding": "base64", "text": "AP8="}))
        report = build_preflight(source, wanted)
        self.assertEqual([r["nearMatches"] for r in report["requests"]],
                         [[candidate(0, "method")], [candidate(0, "body")],
                          [candidate(1, "method")], [candidate(2, "method")]])

    def test_matches_ambiguities_and_unrelated_or_empty_hars_have_no_candidates(self):
        source = har([entry("http://fixture.test/a", "POST", "wrong"),
                      entry("http://fixture.test/a"), entry("http://fixture.test/a"),
                      entry("http://fixture.test/b"), entry("http://fixture.test/b", "POST")])
        wanted = inventory(request("ambiguous"), request("matched", "http://fixture.test/b"),
                           request("unrelated", "http://fixture.test/c"))
        report = build_preflight(source, wanted)
        self.assertEqual([r["status"] for r in report["requests"]], ["ambiguous", "matched", "missing"])
        self.assertEqual([r["nearMatches"] for r in report["requests"]], [[], [], []])
        self.assertEqual(build_preflight(har([]), wanted)["requests"][0]["nearMatches"], [])

    def test_guarded_plan_shares_diagnostics_and_apply_keeps_candidates_as_nonmatches(self):
        source = har([entry("https://stage.example/a?cacheBust=1", "POST", "wrong")])
        wanted = inventory(request("missing"))
        saved = copy.deepcopy((source, wanted, RULES))
        plan = build_plan(source, RULES, inventory=wanted)
        self.assertEqual(plan["requestPreflight"], build_preflight(source, wanted, RULES))
        self.assertEqual(plan["requestPreflight"]["requests"][0]["nearMatches"],
                         [candidate(0, "method", "body")])
        self.assertFalse(plan["safeToApply"])
        with self.assertRaisesRegex(DoctorError, r"missing.*nearMatches.*\/log/entries/0.*method.*body"):
            rewrite_har(source, RULES, inventory=wanted)
        self.assertEqual((source, wanted, RULES), saved)


if __name__ == "__main__":
    unittest.main()
