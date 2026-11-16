"""Synthetic pinned-format migration diagnostics; never infer rule equivalence."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import qawrench.a11y as bridge
from qawrench.a11y.core import canonical

ROOT = Path(__file__).resolve().parents[2]


def report(version="4.10.3", rule="legacy-name", targets=None):
    value = json.loads((ROOT / "examples/a11y/axe-before.json").read_text())
    value["testEngine"]["version"] = version
    value["violations"][0]["id"] = rule
    node = value["violations"][0]["nodes"][0]
    value["violations"][0]["nodes"] = [dict(deepcopy(node), target=t)
        for t in (targets if targets is not None else [["#shared"], ["#removed"]])]
    return value


def snapshot(value):
    return bridge.make_baseline(value, "axe", url="https://fixture.test")


def plan(fmt="axe", old="4.10.3", new="4.11.0", engine="axe"):
    return {"schemaVersion": 1, "kind": "a11y-rule-migration",
            "baseline": {"format": fmt, "inputVersion": old, "ruleSet": "fixture-rules-v1"},
            "current": {"format": fmt, "inputVersion": new, "ruleSet": "fixture-rules-v2"},
            "renames": [{"engine": engine, "from": "legacy-name", "to": "replacement-name",
                         "reason": "Synthetic custom-rule rename, not an upstream alias"}]}


class MigrationTests(unittest.TestCase):
    def diagnose(self, before=None, after=None, mapping=None):
        return bridge.diagnose_migration(snapshot(before if before is not None else report()),
            snapshot(after if after is not None else report("4.11.0", "replacement-name", [["#shared"], ["#added"]])),
            plan() if mapping is None else mapping)

    def test_version_change_pairs_only_declared_rename_at_same_target(self):
        mapping = plan()
        original = deepcopy(mapping)
        result = self.diagnose(mapping=mapping)
        self.assertEqual(mapping, original)
        self.assertEqual(result["kind"], "a11y-migration-diagnostics")
        self.assertEqual(result["counts"], dict(baseline=2, current=2, renamed=1, unchanged=0, added=1, removed=1))
        pair = result["renamed"][0]
        self.assertEqual(pair["baseline"]["target"], ["#shared"])
        self.assertEqual(pair["current"]["rule"], "replacement-name")
        self.assertNotEqual(pair["baseline"]["fingerprint"], pair["current"]["fingerprint"])
        self.assertEqual(result["removed"][0]["target"], ["#removed"])
        self.assertEqual(result["added"][0]["target"], ["#added"])
        self.assertEqual(result["migration"]["renames"][0]["matchedOccurrences"], 1)
        self.assertEqual(result["sources"]["current"]["inputVersion"], "4.11.0")
        self.assertNotIn("gate", result)

    def test_no_map_means_no_inferred_alias_and_unused_map_is_audited(self):
        mapping = plan()
        mapping["renames"] = []
        result = self.diagnose(mapping=mapping)
        self.assertEqual((result["counts"]["renamed"], result["counts"]["added"], result["counts"]["removed"]), (0, 2, 2))
        mapping["renames"] = [{**plan()["renames"][0], "from": "absent-rule"}]
        result = self.diagnose(mapping=mapping)
        self.assertEqual(result["migration"]["renames"][0]["matchedOccurrences"], 0)
        self.assertEqual(result["migration"]["renames"][0]["status"], "unmatched")

    def test_duplicates_full_nested_targets_and_input_order_are_preserved(self):
        nested = ["iframe#checkout", ["checkout-shell", "button.buy"]]
        flat = ["iframe#checkout", "checkout-shell", "button.buy"]
        before = report(targets=[nested, nested, ["#other"]])
        after = report("4.11.0", "replacement-name", [nested, flat, ["#other"], ["#other"]])
        result = self.diagnose(before, after)
        self.assertEqual(result["counts"], dict(baseline=3, current=4, renamed=2, unchanged=0, added=2, removed=1))
        self.assertEqual(result["removed"][0]["target"], nested)
        before["violations"][0]["nodes"].reverse()
        after["violations"][0]["nodes"].reverse()
        # Native source order is retained, while diagnostic pairing is deterministic.
        replay = self.diagnose(before, after)
        for key in ("counts", "renamed", "added", "removed", "unchanged", "migration"):
            self.assertEqual(canonical(result[key]), canonical(replay[key]))

    def test_unchanged_metadata_pairing_and_normal_diff_version_guard(self):
        before = report(rule="same-rule", targets=[["#shared"]])
        after = report("4.11.0", "same-rule", [["#shared"]])
        after["violations"][0]["nodes"][0]["failureSummary"] = "Updated message"
        result = self.diagnose(before, after)
        self.assertEqual(result["counts"]["unchanged"], 1)
        self.assertEqual(result["unchanged"][0]["current"]["message"], "Updated message")
        with self.assertRaisesRegex(bridge.BridgeError, "inputVersion changed"):
            bridge.compare(snapshot(before), snapshot(after))
        self.assertEqual(bridge.validate_baseline(snapshot(after)), snapshot(after))
        with self.assertRaises(bridge.BridgeError):
            bridge.make_baseline(after, "axe", url="https://fixture.test", input_version="4.10.3")

    def test_clean_current_report_means_removed_occurrences_not_proven_rule_retirement(self):
        after = report("4.11.0", "replacement-name")
        after["violations"] = []
        result = self.diagnose(after=after)
        self.assertEqual(result["counts"], dict(baseline=2, current=0, renamed=0, unchanged=0, added=0, removed=2))
        self.assertEqual(result["migration"]["renames"][0]["status"], "unmatched")
        self.assertNotIn("fixed", result)

    def test_pa11y_same_ci_version_rule_set_labels_namespaces_and_empty_targets(self):
        before = json.loads((ROOT / "examples/a11y/pa11y.json").read_text())
        issues = before["results"]["https://fixture.test/"]
        for issue in issues:
            issue.update(code="legacy-name", selector="")
        after = deepcopy(before)
        after["results"]["https://fixture.test/"][0]["code"] = "replacement-name"
        mapping = plan("pa11y-ci", "4.0.0", "4.0.0", "pa11y-ci/htmlcs")
        baseline, current = [bridge.make_baseline(r, "pa11y-ci", input_version="4.0.0") for r in (before, after)]
        result = bridge.diagnose_migration(baseline, current, mapping)
        self.assertEqual((result["counts"]["renamed"], result["counts"]["unchanged"]), (1, 1))
        self.assertEqual(result["renamed"][0]["current"]["target"], "")
        self.assertEqual(result["unchanged"][0]["current"]["engine"], "pa11y-ci/axe")
        self.assertEqual(result["migration"]["current"]["ruleSet"], "fixture-rules-v2")

    def test_bad_maps_ambiguous_aliases_and_mismatched_context_fail(self):
        bad_maps = [None, {}, {**plan(), "schemaVersion": True}, {**plan(), "extra": 1},
                    {**plan(), "renames": {}}, {**plan(), "renames": [None]}]
        for changes in ({"engine": "pa11y-ci/axe"}, {"engine": []}, {"from": ""},
                        {"to": "legacy-name"}, {"reason": " "}, {"extra": 1}):
            bad_maps.append({**plan(), "renames": [{**plan()["renames"][0], **changes}]})
        for extra in (plan()["renames"][0], {**plan()["renames"][0], "to": "third"},
                      {**plan()["renames"][0], "from": "third"},
                      {**plan()["renames"][0], "from": "replacement-name", "to": "third"}):
            bad_maps.append({**plan(), "renames": [plan()["renames"][0], extra]})
        for side in ("baseline", "current"):
            for changes in ({"format": "pa11y-ci"}, {"inputVersion": "9.9.9"},
                            {"inputVersion": "4.11.0" if side == "baseline" else "4.10.3"},
                            {"ruleSet": " "}, {"extra": 1}):
                bad_maps.append({**plan(), side: {**plan()[side], **changes}})
        for mapping in bad_maps:
            with self.subTest(mapping=mapping), self.assertRaises(bridge.BridgeError):
                bridge.diagnose_migration(snapshot(report()), snapshot(report("4.11.0", "replacement-name")), mapping)
        # Coexistence makes a rename ambiguous; never merge independent rule IDs.
        for before, after in ((report(rule="replacement-name"), report("4.11.0", "replacement-name")),
                              (report(), report("4.11.0", "legacy-name"))):
            with self.assertRaisesRegex(bridge.BridgeError, "ambiguous"):
                self.diagnose(before, after)

    def test_failed_incomplete_coverage_options_and_tampered_sources_fail(self):
        for change in (lambda r: r.update(error={}), lambda r: r.update(incomplete=[{}]),
                       lambda r: r.update(url="https://fixture.test/other"),
                       lambda r: r["toolOptions"].update(runOnly=["replacement-name"]),
                       lambda r: r["testEngine"].update(version="4.99.0")):
            after = report("4.11.0", "replacement-name")
            change(after)
            with self.subTest(change=change), self.assertRaises(bridge.BridgeError):
                self.diagnose(after=after)
        baseline = snapshot(report())
        baseline["findings"] = []
        with self.assertRaises(bridge.BridgeError):
            bridge.diagnose_migration(baseline, snapshot(report("4.11.0")), plan())


class MigrationCliTests(unittest.TestCase):
    def test_cli_diagnostics_dry_run_failures_and_map_input_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            baseline, native, mapping, out = [path / n for n in ("baseline.json", "report.json", "map.json", "out.json")]
            baseline.write_text(json.dumps(snapshot(report())))
            native.write_text(json.dumps(report("4.11.0", "replacement-name", [["#shared"], ["#added"]])))
            mapping.write_text(json.dumps(plan()))
            common = [sys.executable, "-m", "qawrench.a11y", "migration", "--format", "axe",
                      "--url", "https://fixture.test", "--baseline", str(baseline), str(native), "--rule-map", str(mapping)]

            def run(*args, expected=0):
                result = subprocess.run([*common, *map(str, args)], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                return result

            summary = json.loads(run("--out", out, "--dry-run").stdout)
            self.assertEqual(summary["counts"]["renamed"], 1)
            self.assertFalse(out.exists())
            run("--out", out)
            self.assertEqual(json.loads(out.read_text())["counts"]["added"], 1)
            original = mapping.read_bytes()
            alias = path / "alias.json"
            alias.hardlink_to(mapping)
            for dest in (mapping, alias, baseline, native):
                run("--out", dest, expected=2)
                self.assertEqual(mapping.read_bytes(), original)
            out.unlink()
            for contents in ("null", '{}', '{"renames":[],"renames":[]}', '{"schemaVersion":NaN}'):
                mapping.write_text(contents)
                run("--out", out, expected=2)
                self.assertFalse(out.exists())
            mapping.write_bytes(original)
            for value in (dict(report("4.11.0"), incomplete=[{}]), dict(report("4.11.0"), scanError={})):
                native.write_text(json.dumps(value))
                run("--out", out, expected=2)
                self.assertFalse(out.exists())

    def test_pa11y_cli_scan_failure_is_not_a_removed_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            baseline = bridge.make_baseline(json.loads((ROOT / "examples/a11y/pa11y.json").read_text()), "pa11y-ci", input_version="4.0.0")
            (path / "baseline.json").write_text(json.dumps(baseline))
            (path / "map.json").write_text(json.dumps(plan("pa11y-ci", "4.0.0", "4.0.0", "pa11y-ci/htmlcs")))
            result = subprocess.run([sys.executable, "-m", "qawrench.a11y", "migration", "--format", "pa11y-ci",
                "--input-version", "4.0.0", "--baseline", str(path / "baseline.json"), "--rule-map", str(path / "map.json"),
                str(ROOT / "examples/a11y/pa11y-scan-failure.json"), "--out", str(path / "out.json")], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("scan failure", result.stderr)
            self.assertFalse((path / "out.json").exists())


if __name__ == "__main__":
    unittest.main()
