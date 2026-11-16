"""Request inventory preflight acceptance tests, using local synthetic HARs."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from examples.har.fixtures import RULES, entry, example_har, har
import qawrench.har as doctor

ROOT = Path(__file__).resolve().parents[2]


def inventory(*requests):
    return {"schemaVersion": 1, "requests": list(requests)}


def request(identifier, url="http://fixture.test/a", method="GET", **fields):
    return {"id": identifier, "url": url, "method": method, **fields}


class PreflightTests(unittest.TestCase):
    def test_precise_missing_unique_and_ambiguous_matches_are_pure(self):
        source = har([entry("http://fixture.test/a"), entry("http://fixture.test/b"),
                      entry("http://fixture.test/a")])
        wanted = inventory(request("duplicate"), request("unique", "http://fixture.test/b"),
                           request("absent", "http://fixture.test/missing"))
        saved = copy.deepcopy((source, wanted))
        report = doctor.build_preflight(source, wanted)
        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"], {"requests": 3, "matchedRequests": 1,
                                              "missingRequests": 1, "ambiguousRequests": 1})
        self.assertEqual([(r["requestId"], r["status"], r["entryIndexes"], r["entryIds"])
                          for r in report["requests"]], [
            ("duplicate", "ambiguous", [0, 2], ["/log/entries/0", "/log/entries/2"]),
            ("unique", "matched", [1], ["/log/entries/1"]),
            ("absent", "missing", [], [])])
        self.assertEqual(report, doctor.build_preflight(source, wanted))
        self.assertEqual((source, wanted), saved)
        self.assertEqual(report["harSHA256"], doctor.build_plan(source, {})["harSHA256"])
        self.assertEqual(doctor.build_preflight(har([]), inventory(request("empty")))["requests"][0]["status"], "missing")

    def test_projection_only_rewrites_fixture_and_uses_exact_method_body_bytes(self):
        source = har([entry("https://stage.example/a?cacheBust=1", "POST", '{"a":1}'),
                      entry("https://stage.example/a?cacheBust=2", "POST", '{ "a":1}'),
                      entry("https://stage.example/a", "GET", "body"),
                      entry("https://stage.example/a", "GET")])
        wanted = inventory(request("post", method="POST", postData={
            "mimeType": "application/json", "text": "eyJhIjoxfQ==", "encoding": "base64"}),
            request("spaced", method="POST", postData={"mimeType": "application/json", "text": '{ "a":1}'}),
            request("get-body", postData={"mimeType": "text/plain", "text": "body"}),
            request("get-empty"), request("case", method="get"),
            request("raw", "https://stage.example/a?cacheBust=1", method="POST",
                    postData={"mimeType": "application/json", "text": '{"a":1}'}))
        saved = copy.deepcopy((source, wanted, RULES))
        report = doctor.build_preflight(source, wanted, RULES)
        self.assertEqual([r["entryIndexes"] for r in report["requests"]], [[0], [1], [2], [3], [], []])
        self.assertEqual(report["requests"][0]["key"], {
            "url": "http://fixture.test/a", "method": "POST", "bodyBase64": "eyJhIjoxfQ=="})
        self.assertEqual((source, wanted, RULES), saved)

    def test_inventory_gates_apply_even_for_identical_response_duplicates(self):
        source = har([entry("https://stage.example/a?cacheBust=1"), entry("http://fixture.test/a")])
        wanted = inventory(request("a"))
        self.assertTrue(doctor.build_plan(source, RULES)["safeToApply"])
        plan = doctor.build_plan(source, RULES, inventory=wanted)
        self.assertFalse(plan["safeToApply"])
        self.assertEqual(plan["requestPreflight"], doctor.build_preflight(source, wanted, RULES))
        with self.assertRaisesRegex(doctor.DoctorError, "request preflight failed.*a.*ambiguous.*0.*1"):
            doctor.rewrite_har(source, RULES, inventory=wanted)
        unique = har([source["log"]["entries"][0]])
        result, plan = doctor.rewrite_har(unique, RULES, inventory=wanted)
        self.assertTrue(plan["safeToApply"])
        self.assertEqual(result["log"]["entries"][0]["request"]["url"], "http://fixture.test/a")
        with self.assertRaisesRegex(doctor.DoctorError, "request preflight failed.*absent.*missing"):
            doctor.rewrite_har(unique, RULES, inventory=inventory(request("absent", "http://fixture.test/absent")))

    def test_preflight_pass_does_not_override_unrelated_response_conflicts(self):
        source = har([entry("https://stage.example/a"), entry("https://stage.example/b"),
                      entry("https://stage.example/b", response_text="different")])
        wanted = inventory(request("a"))
        plan = doctor.build_plan(source, RULES, inventory=wanted)
        self.assertTrue(plan["requestPreflight"]["passed"])
        self.assertFalse(plan["safeToApply"])
        with self.assertRaisesRegex(doctor.DoctorError, "conflicting rewritten request keys"):
            doctor.rewrite_har(source, RULES, inventory=wanted)

    def test_invalid_inventory_fails_closed_with_input_locations(self):
        cases = [(None, "inventory"), ([], "inventory"),
                 ({"requests": [request("a")]}, "schemaVersion"),
                 ({"schemaVersion": True, "requests": []}, "schemaVersion"),
                 ({"schemaVersion": 2, "requests": []}, "schemaVersion"),
                 ({"schemaVersion": 1, "requests": []}, "requests"),
                 ({"schemaVersion": 1, "requests": {}, "extra": 1}, "unknown fields"),
                 (inventory(request("same"), request("same")), "duplicate.*id"),
                 (inventory(request("")), r"requests\[0\].id"),
                 (inventory(None), r"requests\[0\]"),
                 (inventory(request("a", headers=[{"name": "X"}])), "headers"),
                 (inventory(request("a", "/relative")), r"requests\[0\].url"),
                 (inventory(request("a", method="GET\n")), r"requests\[0\].method"),
                 (inventory(request("a", typo=True)), "unknown fields"),
                 (inventory(request("a", bodySize=10)), "postData"),
                 (inventory(request("a", postData={"mimeType": "text/plain", "params": []})), "embedded text"),
                 (inventory(request("a", postData={"mimeType": "text/plain", "text": "!!", "encoding": "base64"})), "base64"),
                 (inventory(request("a", postData={"mimeType": "text/plain", "text": "x", "_file": "secret"})), "attached")]
        for wanted, message in cases:
            with self.subTest(wanted=wanted), self.assertRaisesRegex(doctor.DoctorError, message):
                doctor.build_preflight(example_har(), wanted)


class PreflightCLITests(unittest.TestCase):
    def test_preflight_reports_and_apply_guard_end_to_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.har"
            rules = root / "rules.json"
            wanted = root / "requests.json"
            source.write_text(json.dumps(har([entry("https://stage.example/a?cacheBust=1")])))
            rules.write_text(json.dumps(RULES))
            wanted.write_text(json.dumps(inventory(request("a"))))
            originals = [p.read_bytes() for p in (source, rules, wanted)]

            def cli(command, output, *options):
                return subprocess.run([sys.executable, "-m", "qawrench.har", command, str(source),
                                       "--inventory", str(wanted), "--out", str(output), *map(str, options)],
                                      cwd=ROOT, capture_output=True, text=True, timeout=20)

            missing = root / "missing.json"
            run = cli("preflight", missing)
            self.assertEqual(run.returncode, 2, run.stderr)
            self.assertEqual(json.loads(missing.read_text())["requests"][0]["status"], "missing")
            passed = root / "passed.json"
            run = cli("preflight", passed, "--rules", rules)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(json.loads(run.stdout)["passed"])
            applied = root / "output.har"
            run = cli("apply", applied, "--rules", rules)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(applied.read_text())["log"]["entries"][0]["request"]["url"], "http://fixture.test/a")
            self.assertEqual([p.read_bytes() for p in (source, rules, wanted)], originals)

            wanted.write_text(json.dumps(inventory(request("missing", "http://fixture.test/absent"))))
            blocked = root / "blocked.har"
            run = cli("apply", blocked, "--rules", rules)
            self.assertEqual(run.returncode, 2)
            self.assertIn("missing", run.stderr)
            self.assertFalse(blocked.exists())
            plan = root / "plan.json"
            run = cli("plan", plan, "--rules", rules)
            self.assertEqual(run.returncode, 2)
            self.assertFalse(json.loads(plan.read_text())["safeToApply"])
            self.assertEqual(cli("preflight", passed).returncode, 2)  # no overwrite
            self.assertEqual(cli("preflight", wanted).returncode, 2)  # inventory protected
            source.write_text(json.dumps(har([entry("https://stage.example/a?cacheBust=1"),
                                             entry("https://stage.example/a?cacheBust=2")])))
            wanted.write_text(json.dumps(inventory(request("a"))))
            ambiguous = root / "ambiguous.json"
            run = cli("preflight", ambiguous, "--rules", rules)
            self.assertEqual(run.returncode, 2, run.stderr)
            match = json.loads(ambiguous.read_text())["requests"][0]
            self.assertEqual((match["status"], match["entryIds"]),
                             ("ambiguous", ["/log/entries/0", "/log/entries/1"]))
            self.assertEqual(cli("apply", blocked, "--rules", rules).returncode, 2)
            self.assertFalse(blocked.exists())
            wanted.write_text("null")
            self.assertEqual(cli("apply", blocked, "--rules", rules).returncode, 2)
            self.assertFalse(blocked.exists())
            rules.write_text("null")
            null_rules = root / "null-rules.json"
            run = cli("preflight", null_rules, "--rules", rules)
            self.assertEqual(run.returncode, 2)
            self.assertIn("rules: expected an object", run.stderr)
            self.assertFalse(null_rules.exists())
            rules.write_text(json.dumps(RULES))
            wanted.write_text('{"schemaVersion":1,"requests":[],"requests":[]}')
            invalid = root / "invalid.json"
            run = cli("preflight", invalid)
            self.assertEqual(run.returncode, 2)
            self.assertIn("duplicate JSON object key", run.stderr)
            self.assertFalse(invalid.exists())


if __name__ == "__main__":
    unittest.main()
