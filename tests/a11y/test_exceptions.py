"""Reviewed exceptions never erase findings or grant unbounded allowances."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jsonschema import Draft7Validator, FormatChecker

from qawrench.a11y import BridgeError, compare, make_baseline, to_sarif
from qawrench.a11y.core import POLICY, canonical

ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 11, 11)


def snapshot(name="axe-after.json", *, copies=1, clean=False):
    report = json.loads((ROOT / "examples" / "a11y" / name).read_text())
    if clean:
        report["violations"] = []
    else:
        report["violations"][0]["nodes"] *= copies
    return make_baseline(report, "axe", url="https://fixture.test")


def reviewed(finding=None, **changes):
    f = finding or snapshot()["findings"][0]
    entry = {key: deepcopy(f[key]) for key in ("engine", "page", "rule", "target")}
    entry.update(owner="fixture-team", reason="Synthetic review pending asset replacement",
                 expires="2026-12-01", maxOccurrences=1)
    entry.update(changes)
    return {"schemaVersion": 1, "kind": "a11y-reviewed-exceptions", "identityPolicy": POLICY,
            "exceptions": [entry]}


class ReviewedExceptionTests(unittest.TestCase):
    def delta(self, exceptions, *, baseline=None, current=None, fail_on="serious"):
        return compare(baseline or snapshot(clean=True), current or snapshot(),
                       exceptions=exceptions, fail_on=fail_on, evaluation_date=TODAY)

    def test_active_review_passes_gate_without_erasing_findings(self):
        policy = reviewed()
        original = deepcopy(policy)
        delta = self.delta(policy)
        self.assertEqual(policy, original)
        self.assertEqual(delta["counts"]["new"], 1)
        self.assertEqual(delta["gate"], {"failOn": "serious", "passed": True,
                         "newAtOrAboveThreshold": 1, "exceptedNewAtOrAboveThreshold": 1,
                         "unexceptedNewAtOrAboveThreshold": 0})
        audit = delta["exceptions"]
        self.assertEqual(audit["evaluatedOn"], "2026-11-11")
        entry = audit["entries"][0]
        self.assertEqual((entry["status"], entry["currentOccurrences"], entry["exceptedNewOccurrences"]),
                         ("active", 1, 1))
        self.assertEqual(delta["new"][0]["reviewedException"]["owner"], "fixture-team")
        self.assertEqual(delta["new"][0]["native"], snapshot()["findings"][0]["native"])

    def test_expiry_boundary_and_unmatched_reviews_are_visible_but_do_not_gate(self):
        for changes, status in (({"expires": "2026-11-10"}, "expired"),
                                ({"expires": "2026-11-11"}, "expired"),
                                ({"target": ["#other"]}, "unmatched"),
                                ({"page": "https://fixture.test/other"}, "unmatched"),
                                ({"rule": "other-rule"}, "unmatched")):
            with self.subTest(changes=changes):
                delta = self.delta(reviewed(**changes))
                self.assertFalse(delta["gate"]["passed"])
                entry = delta["exceptions"]["entries"][0]
                self.assertEqual(entry["status"], status)
                self.assertEqual(entry["exceptedNewOccurrences"], 0)
                self.assertNotIn("reviewedException", delta["new"][0])
        # A fixed identity is unmatched even though it existed in the baseline.
        delta = self.delta(reviewed(snapshot("axe-before.json")["findings"][0]),
                           baseline=snapshot("axe-before.json"))
        self.assertEqual(delta["exceptions"]["entries"][0]["status"], "unmatched")
        self.assertFalse(delta["gate"]["passed"])

    def test_occurrence_budget_cannot_hide_an_additional_duplicate(self):
        delta = self.delta(reviewed(), current=snapshot(copies=2))
        self.assertEqual(delta["counts"]["new"], 2)
        self.assertEqual(sum("reviewedException" in f for f in delta["new"]), 1)
        self.assertFalse(delta["gate"]["passed"])
        self.assertEqual(delta["gate"]["unexceptedNewAtOrAboveThreshold"], 1)
        # Existing occurrences consume the budget before new ones.
        delta = self.delta(reviewed(), baseline=snapshot(), current=snapshot(copies=2))
        self.assertEqual(delta["exceptions"]["entries"][0]["unchangedOccurrences"], 1)
        self.assertEqual(delta["gate"]["exceptedNewAtOrAboveThreshold"], 0)
        self.assertFalse(delta["gate"]["passed"])
        self.assertTrue(self.delta(reviewed(maxOccurrences=2), baseline=snapshot(),
                                   current=snapshot(copies=2))["gate"]["passed"])

    def test_full_nested_target_and_page_normalization(self):
        current = snapshot("axe-nested.json")
        clean = snapshot("axe-nested.json", clean=True)
        policy = reviewed(current["findings"][0], page="HTTPS://FIXTURE.TEST:443/checkout#payment")
        self.assertTrue(self.delta(policy, baseline=clean, current=current)["gate"]["passed"])
        policy["exceptions"][0]["target"] = ["iframe#checkout", "checkout-shell", "button.buy"]
        self.assertFalse(self.delta(policy, baseline=clean, current=current)["gate"]["passed"])

    def test_pa11y_page_level_target_and_engine_namespace(self):
        report = json.loads((ROOT / "examples/a11y/pa11y.json").read_text())
        issue = report["results"]["https://fixture.test/"][0]
        issue["selector"] = ""
        current = make_baseline(report, "pa11y-ci", input_version="4.0.0")
        report["results"]["https://fixture.test/"] = []
        report.update(passes=2, errors=0)
        clean = make_baseline(report, "pa11y-ci", input_version="4.0.0")
        f = next(f for f in current["findings"] if f["severity"] == "serious")
        policy = reviewed(f)
        self.assertTrue(self.delta(policy, baseline=clean, current=current)["gate"]["passed"])
        policy["exceptions"][0]["engine"] = "pa11y-ci/axe"
        self.assertFalse(self.delta(policy, baseline=clean, current=current)["gate"]["passed"])

    def test_malformed_or_ambiguous_reviews_rejected(self):
        changes = [{"owner": " "}, {"reason": ""}, {"expires": "2026-02-30"},
                   {"expires": "20261201"}, {"expires": "2026-12-01T00:00:00Z"},
                   {"expires": None}, {"maxOccurrences": True}, {"maxOccurrences": 0},
                   {"maxOccurrences": 1.5}, {"engine": "*"}, {"engine": []},
                   {"target": "#new-promo"}, {"target": []}, {"rule": ""},
                   {"page": "/relative"}, {"expiry": "2026-12-01"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(BridgeError):
                self.delta(reviewed(**change))
        for key in reviewed()["exceptions"][0]:
            policy = reviewed()
            del policy["exceptions"][0][key]
            with self.subTest(missing=key), self.assertRaises(BridgeError):
                self.delta(policy)
        bad_files = [[], {}, {**reviewed(), "schemaVersion": True},
                     {**reviewed(), "schemaVersion": 2}, {**reviewed(), "kind": "other"},
                     {**reviewed(), "identityPolicy": "other"}, {**reviewed(), "unknown": 1},
                     {**reviewed(), "exceptions": {}}, {**reviewed(), "exceptions": [None]}]
        duplicate = reviewed()
        duplicate["exceptions"].append({**duplicate["exceptions"][0], "page": "HTTPS://FIXTURE.TEST:443"})
        bad_files.append(duplicate)
        for policy in bad_files:
            with self.subTest(policy=policy), self.assertRaises(BridgeError):
                self.delta(policy)

    def test_empty_reviews_and_report_only_keep_raw_counts(self):
        policy = {**reviewed(), "exceptions": []}
        self.assertFalse(self.delta(policy)["gate"]["passed"])
        self.assertEqual(self.delta(policy)["exceptions"]["entries"], [])
        delta = self.delta(reviewed(), fail_on="none")
        self.assertTrue(delta["gate"]["passed"])
        self.assertEqual(delta["counts"]["new"], 1)
        self.assertEqual(delta["exceptions"]["entries"][0]["exceptedNewOccurrences"], 1)

    def test_sarif_retains_all_findings_and_entire_review_audit(self):
        policy = reviewed()
        policy["exceptions"] += reviewed(target=["#gone"])["exceptions"]
        delta = self.delta(policy, current=snapshot(copies=2))
        sarif = to_sarif(delta)
        schema = json.loads((ROOT / "src/qawrench/a11y/schemas/sarif-output.schema.json").read_text())
        Draft7Validator(schema, format_checker=FormatChecker()).validate(sarif)
        run = sarif["runs"][0]
        self.assertEqual(run["properties"]["exceptions"], delta["exceptions"])
        self.assertEqual(len(run["results"]), 2)
        self.assertEqual({r["baselineState"] for r in run["results"]}, {"new"})
        self.assertEqual(sum("reviewedException" in r["properties"] for r in run["results"]), 1)
        self.assertEqual(canonical(sarif), canonical(to_sarif(delta)))


class ReviewedExceptionCliTests(unittest.TestCase):
    def test_cli_review_gate_audit_dry_run_invalid_input_and_path_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            baseline, policy, out, sarif = [path / name for name in ("baseline.json", "exceptions.json", "delta.json", "out.sarif")]
            baseline.write_text(json.dumps(snapshot(clean=True)))
            policy.write_text(json.dumps(reviewed(expires="9999-12-31")))
            common = [sys.executable, "-m", "qawrench.a11y", "diff", "--format", "axe",
                      "--url", "https://fixture.test", "--baseline", str(baseline),
                      str(ROOT / "examples/a11y/axe-after.json"), "--exceptions", str(policy), "--fail-on", "serious"]

            def run(*args, expected):
                result = subprocess.run([*common, *map(str, args)], cwd=ROOT, text=True, capture_output=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                return result

            summary = json.loads(run("--out", out, "--sarif", sarif, "--dry-run", expected=0).stdout)
            self.assertFalse(out.exists())
            self.assertFalse(sarif.exists())
            self.assertEqual(summary["exceptions"]["active"], 1)
            run("--out", out, "--sarif", sarif, expected=0)
            self.assertEqual(json.loads(out.read_text())["counts"]["new"], 1)
            self.assertEqual(json.loads(sarif.read_text())["runs"][0]["properties"]["exceptions"]["counts"]["active"], 1)
            original = policy.read_bytes()
            alias = path / "alias.json"
            alias.hardlink_to(policy)
            for args in (("--out", policy), ("--out", alias), ("--out", out, "--sarif", policy)):
                run(*args, expected=2)
                self.assertEqual(policy.read_bytes(), original)
            out.unlink()
            sarif.unlink()
            for value in ("null", '{"schemaVersion":1,"schemaVersion":1}', '{"schemaVersion":NaN}', '{}'):
                policy.write_text(value)
                run("--out", out, "--sarif", sarif, expected=2)
                self.assertFalse(out.exists())
                self.assertFalse(sarif.exists())
            for entry, status in ((reviewed(expires="2000-01-01"), "expired"),
                                  (reviewed(target=["#gone"], expires="9999-12-31"), "unmatched")):
                policy.write_text(json.dumps(entry))
                summary = json.loads(run("--out", out, "--sarif", sarif, expected=1).stdout)
                self.assertEqual(summary["exceptions"][status], 1)
                self.assertEqual(json.loads(out.read_text())["exceptions"]["entries"][0]["status"], status)


if __name__ == "__main__":
    unittest.main()
