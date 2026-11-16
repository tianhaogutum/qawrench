"""Offline substantive adapter, multiset, SARIF and CLI contract tests."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jsonschema import Draft7Validator, FormatChecker

from qawrench.a11y import BridgeError, compare, make_baseline, to_sarif, validate_baseline
from qawrench.a11y.core import canonical, normalize_url

ROOT = Path(__file__).resolve().parents[2]


def fixture(name):
    return json.loads((ROOT / "examples" / "a11y" / name).read_text())


def axe(report=None, **kwargs):
    return make_baseline(report if report is not None else fixture("axe-before.json"), "axe", url="https://fixture.test", **kwargs)


def pa11y(report=None):
    return make_baseline(report if report is not None else fixture("pa11y.json"), "pa11y-ci", input_version="4.0.0")


class AdapterTests(unittest.TestCase):
    def test_axe_native_values_and_unknown_metadata_retained(self):
        report = fixture("axe-before.json")
        report["customMetadata"] = {"example": [1, 2]}
        snapshot = axe(report)
        f = snapshot["findings"][0]
        self.assertEqual(f["engine"], "axe")
        self.assertEqual(f["page"], "https://fixture.test/")
        self.assertEqual(f["rule"], "image-alt")
        self.assertEqual(f["impact"], "critical")
        self.assertEqual(f["message"], report["violations"][0]["nodes"][0]["failureSummary"])
        self.assertEqual(f["native"]["node"], report["violations"][0]["nodes"][0])
        self.assertEqual(snapshot["source"]["report"], report)

    def test_nested_frame_shadow_round_trip_and_no_aliasing(self):
        report = fixture("axe-nested.json")
        snapshot = axe(report)
        target = ["iframe#checkout", ["checkout-shell", "button.buy"]]
        self.assertEqual(snapshot["findings"][0]["target"], target)
        self.assertEqual(validate_baseline(json.loads(json.dumps(snapshot))), snapshot)
        flattened = deepcopy(report)
        flattened["violations"][0]["nodes"][0]["target"] = ["iframe#checkout", "checkout-shell", "button.buy"]
        self.assertNotEqual(axe(flattened)["findings"][0]["fingerprint"], snapshot["findings"][0]["fingerprint"])
        report["violations"][0]["nodes"][0]["target"][1][0] = "mutated"
        self.assertEqual(snapshot["findings"][0]["target"], target)

    def test_pa11y_combined_map_runners_levels_and_native_fields(self):
        snapshot = pa11y()
        self.assertEqual(snapshot["pages"], ["https://fixture.test/", "https://fixture.test/about"])
        by_engine = {f["engine"]: f for f in snapshot["findings"]}
        self.assertEqual(set(by_engine), {"pa11y-ci/htmlcs", "pa11y-ci/axe"})
        self.assertEqual(by_engine["pa11y-ci/htmlcs"]["severity"], "serious")
        self.assertIsNone(by_engine["pa11y-ci/htmlcs"]["impact"])
        self.assertEqual(by_engine["pa11y-ci/axe"]["impact"], "moderate")
        for f in snapshot["findings"]:
            self.assertEqual(f["message"], f["native"]["message"])
            self.assertEqual(f["target"], f["native"]["selector"])
            self.assertEqual(f["rule"], f["native"]["code"])

    def test_pa11y_page_level_empty_selector_is_preserved(self):
        report = fixture("pa11y.json")
        report["results"]["https://fixture.test/"][0]["selector"] = ""
        self.assertIn("", [f["target"] for f in pa11y(report)["findings"]])

    def test_conservative_url_normalization(self):
        self.assertEqual(normalize_url("HTTPS://FIXTURE.TEST:443"), "https://fixture.test/")
        self.assertEqual(normalize_url("http://[::1]:80/a"), "http://[::1]/a")
        for suffix in ("?a=1&a=2", "?a=2&a=1", "#view-a", "#view-b", "/a", "/A", "/%61"):
            self.assertTrue(normalize_url("https://fixture.test" + suffix).endswith(suffix))
        for value in (None, "", "/relative", "file:///tmp/test", "https://user:pw@fixture.test", "https://fixture.test:bad", "https://fixture.test/%GG", "https://fixture.test/\n"):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                normalize_url(value)

    def test_missing_url_and_bad_engine_versions_fail(self):
        report = fixture("axe-before.json")
        with self.assertRaisesRegex(BridgeError, "page URL"):
            make_baseline(report, "axe")
        for field, value in (("name", "other"), ("version", "4.99.0"), ("version", "3.0.0")):
            bad = deepcopy(report)
            bad["testEngine"][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(BridgeError, "engine/version"):
                axe(bad)
        with self.assertRaisesRegex(BridgeError, "unknown format"):
            make_baseline(report, "other")
        with self.assertRaisesRegex(BridgeError, "supply --input-version"):
            make_baseline(fixture("pa11y.json"), "pa11y-ci")
        with self.assertRaisesRegex(BridgeError, "unknown pa11y-ci version"):
            make_baseline(fixture("pa11y.json"), "pa11y-ci", input_version="5.0.0")

    def test_axe_incomplete_failure_and_truncation_fail(self):
        with self.assertRaisesRegex(BridgeError, "incomplete"):
            axe(fixture("axe-incomplete.json"))
        changes = [
            lambda x: x.pop("incomplete"),
            lambda x: x.update(error={"message": "scan failed"}),
            lambda x: x.update(complete=False),
            lambda x: x.update(status="failed"),
            lambda x: x["toolOptions"].update(selectors=False),
            lambda x: x["toolOptions"].update(reporter="raw"),
            lambda x: x["toolOptions"].update(resultTypes=["passes"]),
            lambda x: x["violations"][0].update(nodes=[]),
        ]
        for change in changes:
            bad = fixture("axe-before.json")
            change(bad)
            with self.subTest(change=change), self.assertRaises(BridgeError):
                axe(bad)

    def test_library_rejects_non_json_native_data(self):
        for value in (float("nan"), object()):
            report = fixture("axe-before.json")
            report["customData"] = value
            with self.assertRaisesRegex(BridgeError, "serializable JSON"):
                axe(report)

    def test_malformed_targets_fail_without_flattening(self):
        for target in (None, [], "#logo", [None], [42], [{}], [["only-one"]], [["host", ["deep", "leaf"]]], [""], [["host", ""]]):
            report = fixture("axe-before.json")
            report["violations"][0]["nodes"][0]["target"] = target
            with self.subTest(target=target), self.assertRaises(BridgeError):
                axe(report)

    def test_pa11y_failures_incomplete_and_inconsistent_counts_fail(self):
        with self.assertRaisesRegex(BridgeError, "scan failure"):
            pa11y(fixture("pa11y-scan-failure.json"))
        changes = [
            lambda x: x.update(total=3),
            lambda x: x.update(passes=0),
            lambda x: x.update(errors=0),
            lambda x: x.update(total=True),
            lambda x: x["results"].update({"https://fixture.test/about": None}),
            lambda x: x["results"]["https://fixture.test/"][0].update(runner="unknown"),
            lambda x: x["results"]["https://fixture.test/"][0].update(selector=[]),
            lambda x: x["results"]["https://fixture.test/"][0].update(type="unknown"),
            lambda x: x["results"]["https://fixture.test/"][0].update(type=[]),
            lambda x: x["results"].update({"https://fixture.test/": [{}]}),
            lambda x: x["results"]["https://fixture.test/"][0].update(typeCode=3),
            lambda x: x["results"]["https://fixture.test/"][0].update(runnerExtras={"impact": "unrecognized"}),
        ]
        for change in changes:
            bad = fixture("pa11y.json")
            change(bad)
            with self.subTest(change=change), self.assertRaises(BridgeError):
                pa11y(bad)
        collision = {"total": 2, "passes": 2, "errors": 0, "results": {"https://fixture.test": [], "https://fixture.test:443/": []}}
        with self.assertRaisesRegex(BridgeError, "collide"):
            pa11y(collision)
        with self.assertRaises(BridgeError):
            pa11y({"total": 0, "passes": 0, "errors": 0, "results": {}})


class ComparisonTests(unittest.TestCase):
    def test_equal_total_swap_is_new_and_fixed_and_gate_on_new(self):
        delta = compare(axe(), axe(fixture("axe-after.json")), fail_on="critical")
        self.assertEqual(delta["counts"], {"new": 1, "fixed": 1, "unchanged": 0, "baseline": 1, "current": 1})
        self.assertFalse(delta["gate"]["passed"])
        self.assertEqual(delta["fixed"][0]["target"], ["#old-logo"])
        self.assertEqual(delta["new"][0]["target"], ["#new-promo"])

    def test_duplicate_occurrences_are_a_multiset(self):
        report = fixture("axe-before.json")
        node = report["violations"][0]["nodes"][0]
        report["violations"][0]["nodes"] += [deepcopy(node), deepcopy(node)]
        delta = compare(axe(), axe(report))
        self.assertEqual(delta["counts"], {"new": 2, "fixed": 0, "unchanged": 1, "baseline": 1, "current": 3})
        self.assertEqual(len({f["fingerprint"] for f in delta["new"]}), 1)
        reverse = compare(axe(report), axe())
        self.assertEqual((reverse["counts"]["new"], reverse["counts"]["fixed"]), (0, 2))

    def test_pa11y_swap_duplicates_and_runner_namespace(self):
        report = fixture("pa11y.json")
        issues = report["results"]["https://fixture.test/"]
        issues[0]["selector"] = "#new-logo"
        delta = compare(pa11y(), pa11y(report), fail_on="serious")
        self.assertEqual((delta["counts"]["new"], delta["counts"]["fixed"], delta["counts"]["unchanged"]), (1, 1, 1))
        self.assertFalse(delta["gate"]["passed"])
        issues.append(deepcopy(issues[0]))
        report["errors"] += 1
        delta = compare(pa11y(), pa11y(report))
        self.assertEqual(delta["counts"]["new"], 2)
        # Even identical native codes/selectors in different runners stay separate.
        issues[0]["code"] = issues[1]["code"]
        issues[0]["selector"] = issues[1]["selector"]
        snapshot = pa11y(report)
        f = next(f for f in snapshot["findings"] if f["engine"] == "pa11y-ci/htmlcs" and f["rule"] == "color-contrast")
        g = next(f for f in snapshot["findings"] if f["engine"] == "pa11y-ci/axe")
        self.assertNotEqual(f["fingerprint"], g["fingerprint"])

    def test_metadata_change_is_unchanged_with_both_versions(self):
        report = fixture("axe-before.json")
        report["violations"][0]["nodes"][0].update(failureSummary="Changed diagnostic", impact="minor")
        delta = compare(axe(), axe(report), fail_on="minor")
        self.assertEqual(delta["counts"]["unchanged"], 1)
        pair = delta["unchanged"][0]
        self.assertNotEqual(pair["baseline"]["message"], pair["current"]["message"])
        self.assertEqual(pair["current"]["severity"], "minor")
        self.assertTrue(delta["gate"]["passed"])

    def test_exact_duplicate_metadata_matches_first(self):
        report = fixture("axe-before.json")
        other = deepcopy(report["violations"][0]["nodes"][0])
        other["failureSummary"] = "Different message"
        report["violations"][0]["nodes"].append(other)
        delta = compare(axe(report), axe())
        self.assertEqual(delta["unchanged"][0]["baseline"], delta["unchanged"][0]["current"])
        self.assertEqual(delta["fixed"][0]["message"], "Different message")

    def test_severity_gates_on_new_not_existing_and_unknown_is_conservative(self):
        current = fixture("axe-after.json")
        current["violations"][0]["nodes"][0]["impact"] = "moderate"
        self.assertTrue(compare(axe(), axe(current), fail_on="serious")["gate"]["passed"])
        self.assertFalse(compare(axe(), axe(current), fail_on="moderate")["gate"]["passed"])
        current["violations"][0]["nodes"][0]["impact"] = None
        self.assertFalse(compare(axe(), axe(current), fail_on="critical")["gate"]["passed"])
        self.assertTrue(compare(axe(), axe(), fail_on="minor")["gate"]["passed"])
        with self.assertRaises(BridgeError):
            compare(axe(), axe(), fail_on="bogus")

    def test_complete_clean_report_fixes_existing(self):
        clean = fixture("axe-before.json")
        clean["violations"] = []
        delta = compare(axe(), axe(clean), fail_on="minor")
        self.assertEqual((delta["counts"]["new"], delta["counts"]["fixed"]), (0, 1))
        self.assertTrue(delta["gate"]["passed"])

    def test_changed_coverage_options_engine_or_corrupt_baseline_fails(self):
        changed_page = fixture("axe-before.json")
        changed_page["url"] = "https://fixture.test/other"
        changed_options = fixture("axe-before.json")
        changed_options["toolOptions"]["runOnly"] = ["image-alt"]
        for current in (axe(changed_page), axe(changed_options), pa11y()):
            with self.subTest(current=current["pages"]), self.assertRaisesRegex(BridgeError, "incomparable"):
                compare(axe(), current)
        report = fixture("pa11y.json")
        del report["results"]["https://fixture.test/about"]
        report.update(total=1, passes=0)
        with self.assertRaisesRegex(BridgeError, "coverage"):
            compare(pa11y(), pa11y(report))
        bad = axe()
        bad["findings"][0]["fingerprint"] = "0" * 64
        with self.assertRaisesRegex(BridgeError, "do not match"):
            validate_baseline(bad)
        bad = axe()
        bad["source"]["format"] = []
        with self.assertRaises(BridgeError):
            validate_baseline(bad)
        bad = axe()
        bad["schemaVersion"] = 2
        with self.assertRaisesRegex(BridgeError, "schema"):
            validate_baseline(bad)


class SarifTests(unittest.TestCase):
    def test_structural_schema_and_semantics_for_both_formats(self):
        schema = json.loads((ROOT / "src/qawrench/a11y/schemas/sarif-output.schema.json").read_text())
        Draft7Validator.check_schema(schema)
        validator = Draft7Validator(schema, format_checker=FormatChecker())
        for baseline, current in ((axe(), axe(fixture("axe-after.json"))), (pa11y(), pa11y()), (axe(fixture("axe-nested.json")), axe(fixture("axe-nested.json")))):
            sarif = to_sarif(compare(baseline, current))
            validator.validate(sarif)
            run = sarif["runs"][0]
            for result in run["results"]:
                rule = run["tool"]["driver"]["rules"][result["ruleIndex"]]
                self.assertEqual(rule["id"], result["ruleId"])
                self.assertEqual(result["properties"]["pageUrl"], result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"])
                self.assertNotIn("region", result["locations"][0]["physicalLocation"])
                self.assertTrue(rule["helpUri"].startswith("https://"))
        ids = {r["ruleId"] for r in to_sarif(compare(pa11y(), pa11y()))["runs"][0]["results"]}
        self.assertIn("pa11y-ci/axe:color-contrast", ids)
        self.assertTrue(any(i.startswith("pa11y-ci/htmlcs:WCAG2AA.") for i in ids))

    def test_states_duplicates_and_deterministic_fingerprints(self):
        delta = compare(axe(), axe(fixture("axe-after.json")))
        sarif = to_sarif(delta)
        self.assertEqual({r["baselineState"] for r in sarif["runs"][0]["results"]}, {"new", "absent"})
        self.assertEqual(canonical(sarif), canonical(to_sarif(delta)))
        report = fixture("axe-before.json")
        report["violations"][0]["nodes"] *= 2
        results = to_sarif(compare(axe(), axe(report)))["runs"][0]["results"]
        self.assertEqual([r["properties"]["occurrence"] for r in results], [1, 2])
        self.assertEqual(results[0]["partialFingerprints"], results[1]["partialFingerprints"])
        nested = axe(fixture("axe-nested.json"))
        result = to_sarif(compare(nested, nested))["runs"][0]["results"][0]
        self.assertEqual(result["properties"]["selector"], ["iframe#checkout", ["checkout-shell", "button.buy"]])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.baseline = self.path / "baseline.json"
        self.delta = self.path / "delta.json"
        self.sarif = self.path / "report.sarif"

    def cli(self, *args, expected=0):
        p = subprocess.run([sys.executable, "-m", "qawrench.a11y", *map(str, args)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, expected, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def baseline_axe(self):
        return self.cli("baseline", "--format", "axe", "--url", "https://fixture.test", ROOT / "examples/a11y/axe-before.json", "--out", self.baseline)

    def test_axe_cli_e2e_reports_written_before_gate_exit(self):
        self.baseline_axe()
        p = self.cli("diff", "--format", "axe", "--url", "https://fixture.test", "--baseline", self.baseline, ROOT / "examples/a11y/axe-after.json", "--out", self.delta, "--sarif", self.sarif, "--fail-on", "serious", expected=1)
        self.assertFalse(json.loads(p.stdout)["gate"]["passed"])
        self.assertEqual(json.loads(self.delta.read_text())["counts"]["new"], 1)
        self.assertEqual(json.loads(self.delta.read_text())["counts"]["fixed"], 1)
        self.assertEqual(json.loads(self.sarif.read_text())["version"], "2.1.0")
        self.assertFalse(list(self.path.glob("*.tmp")))

    def test_pa11y_cli_e2e_with_combined_report(self):
        report = ROOT / "examples/a11y/pa11y.json"
        self.cli("baseline", "--format", "pa11y-ci", "--input-version", "4.0.0", report, "--out", self.baseline)
        self.cli("diff", "--format", "pa11y-ci", "--input-version", "4.0.0", "--baseline", self.baseline, report, "--out", self.delta, "--sarif", self.sarif, "--fail-on", "minor")
        self.assertEqual(json.loads(self.delta.read_text())["counts"]["unchanged"], 2)
        self.assertTrue(all(r["ruleId"].startswith("pa11y-ci/") for r in json.loads(self.sarif.read_text())["runs"][0]["results"]))

    def test_dry_run_does_not_write_or_execute_anything(self):
        self.baseline_axe()
        original = self.baseline.read_bytes()
        p = self.cli("diff", "--format", "axe", "--url", "https://fixture.test", "--baseline", self.baseline, ROOT / "examples/a11y/axe-after.json", "--out", self.delta, "--sarif", self.sarif, "--dry-run")
        self.assertTrue(json.loads(p.stdout)["dryRun"])
        self.assertFalse(self.delta.exists())
        self.assertFalse(self.sarif.exists())
        self.assertEqual(self.baseline.read_bytes(), original)
        self.cli("baseline", "--format", "axe", "--url", "https://fixture.test", ROOT / "examples/a11y/axe-before.json", "--out", self.delta, "--dry-run")
        self.assertFalse(self.delta.exists())

    def test_bad_json_failed_scan_and_io_errors_create_no_output(self):
        for contents in ('{"violations":', '{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
            bad = self.path / "bad.json"
            bad.write_text(contents)
            self.cli("baseline", "--format", "axe", "--url", "https://fixture.test", bad, "--out", self.baseline, expected=2)
            self.assertFalse(self.baseline.exists())
        for name in ("axe-incomplete.json", "pa11y-scan-failure.json"):
            fmt = "axe" if name.startswith("axe") else "pa11y-ci"
            args = ["--url", "https://fixture.test"] if fmt == "axe" else ["--input-version", "4.0.0"]
            self.cli("baseline", "--format", fmt, *args, ROOT / "examples" / "a11y" / name, "--out", self.baseline, expected=2)
            self.assertFalse(self.baseline.exists())
        self.cli("baseline", "--format", "axe", self.path / "missing.json", "--out", self.baseline, expected=2)
        self.cli("baseline", "--format", "axe", "--url", "https://fixture.test", ROOT / "examples/a11y/axe-before.json", "--out", self.path / "missing/output.json", expected=2)

    def test_protect_input_and_baseline_and_reject_conflicting_outputs(self):
        self.baseline_axe()
        before = self.baseline.read_bytes()
        common = ["diff", "--format", "axe", "--url", "https://fixture.test", "--baseline", self.baseline, ROOT / "examples/a11y/axe-after.json"]
        self.cli(*common, "--out", self.baseline, expected=2)
        self.assertEqual(self.baseline.read_bytes(), before)
        self.cli(*common, "--out", self.delta, "--sarif", self.delta, expected=2)
        self.assertFalse(self.delta.exists())
        hardlink = self.path / "alias.json"
        hardlink.hardlink_to(self.baseline)
        self.cli(*common, "--out", hardlink, expected=2)
        self.assertEqual(self.baseline.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
