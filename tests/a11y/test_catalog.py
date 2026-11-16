"""Synthetic getRules catalogs; observed outcomes never establish remediation."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import qawrench.a11y as bridge
from .test_migration import ROOT, plan, report, snapshot


def catalog(version="4.10.3", rules=("legacy-name",), rule_set="fixture-rules-v1"):
    return {"schemaVersion": 1, "kind": "a11y-rule-catalog", "format": "axe",
            "inputVersion": version, "ruleSet": rule_set,
            "export": {"api": "axe.getRules", "tags": None},
            "rules": [{"ruleId": rule, "description": "Synthetic description", "help": "Synthetic help",
                       "helpUrl": "https://fixture.test/help", "tags": [], "actIds": []} for rule in rules]}


def outcome(rule, group="passes"):
    result = deepcopy(report(rule=rule, targets=[["#elsewhere"]])["violations"][0])
    if group == "inapplicable":
        result.update(nodes=[], impact=None)
    return result


def inputs():
    before = report()
    after = report("4.11.0", "replacement-name", [["#shared"], ["#added"]])
    old = catalog(rules=("legacy-name", "retired-rule", "still-rule", "skipped-rule"))
    new = catalog("4.11.0", ("replacement-name", "still-rule", "skipped-rule"), "fixture-rules-v2")
    for rule in ("retired-rule", "still-rule", "skipped-rule"):
        before["violations"].append(outcome(rule))
    after["inapplicable"] = [outcome("still-rule", "inapplicable")]
    return before, after, old, new


class CatalogTests(unittest.TestCase):
    def diagnose(self, before, after, old, new, mapping=None):
        return bridge.diagnose_migration(snapshot(before), snapshot(after), mapping or plan(),
                                         baseline_catalog=old, current_catalog=new)

    def test_original_target_observations_share_already_unchanged_nodes_without_alias(self):
        nested = ["iframe#checkout", ["checkout-shell", "button.buy"]]
        before = report(targets=[nested, nested, nested, ["#pass-only"], ["#missing"]])
        after = report("4.11.0", targets=[nested, ["#elsewhere"]])
        passed = outcome("legacy-name")
        node = passed["nodes"][0]
        passed["nodes"] = [dict(deepcopy(node), target=t)
                           for t in (nested, nested, ["#pass-only"], ["#pass-only"])]
        after["passes"] = [passed]
        after["inapplicable"] = [outcome("legacy-name", "inapplicable")]
        old, new = catalog(), catalog("4.11.0", ("legacy-name",), "fixture-rules-v2")
        mapping = {**plan(), "renames": []}
        original = deepcopy((before, after, old, new, mapping))
        result = self.diagnose(before, after, old, new, mapping)
        ordinary = bridge.diagnose_migration(snapshot(before), snapshot(after), mapping)
        self.assertEqual(result["counts"], dict(baseline=5, current=2, unchanged=1, renamed=0, added=1, removed=4))
        self.assertEqual(result["unchanged"], ordinary["unchanged"])
        self.assertEqual(result["counts"], ordinary["counts"])
        for finding in result["removed"]:
            counts = ({"violations": 1, "passes": 2} if finding["target"] == nested else
                      {"violations": 0, "passes": 2} if finding["target"] == ["#pass-only"] else
                      {"violations": 0, "passes": 0})
            self.assertEqual(finding["ruleTargetCoverage"], {
                "rule": "legacy-name", "target": finding["target"],
                "status": "observed-target-outcome" if any(counts.values()) else "no-observed-target-outcome",
                "observedNodes": counts})
            self.assertEqual(finding["ruleCoverage"]["outcomes"], ["inapplicable", "passes", "violations"])
            self.assertNotIn("mappedTargetCoverage", finding)
            self.assertNotIn("mappedRuleCoverage", finding)
        nested_leftovers = [f for f in result["removed"] if f["target"] == nested]
        self.assertEqual(len(nested_leftovers), 2)
        self.assertEqual(nested_leftovers[0]["ruleTargetCoverage"], nested_leftovers[1]["ruleTargetCoverage"])
        self.assertNotIn("fixed", result)
        self.assertNotIn("gate", result)
        self.assertEqual((before, after, old, new, mapping), original)
        before["violations"][0]["nodes"].reverse()
        after["violations"][0]["nodes"].reverse()
        after["passes"][0]["nodes"].reverse()
        replay_inputs = deepcopy((before, after, old, new, mapping))
        replay = self.diagnose(before, after, old, new, mapping)
        for key in ("removed", "unchanged", "added", "counts", "migration", "ruleCatalogs"):
            self.assertEqual(result[key], replay[key])
        nested_leftovers[0]["ruleTargetCoverage"]["observedNodes"]["passes"] = 99
        nested_leftovers[0]["ruleTargetCoverage"]["target"][1][0] = "changed"
        self.assertEqual(nested_leftovers[1]["ruleTargetCoverage"]["observedNodes"]["passes"], 2)
        self.assertEqual(nested_leftovers[0]["target"], nested)
        self.assertEqual((before, after, old, new, mapping), replay_inputs)

    def test_original_target_observations_require_literal_rule_and_full_target(self):
        nested = ["iframe#checkout", ["checkout-shell", "button.buy"]]
        before = report(targets=[nested, nested])
        mapping = {**plan(), "renames": []}
        for groups, target, retain_source in (
            ((), nested, True),
            ((), nested, False),
            (("inapplicable",), nested, True),
            (("violations", "passes"), ["#elsewhere"], True),
            (("passes",), ["iframe#checkout", "checkout-shell", "button.buy"], True),
            (("passes",), ["iframe#checkout", ["button.buy", "checkout-shell"]], True),
            (("passes",), [["checkout-shell", "button.buy"], "iframe#checkout"], True),
            (("passes",), ["iframe#checkout", ["checkout-shell", "button.buy "]], True),
            (("passes",), nested, True),
        ):
            with self.subTest(groups=groups, target=target, retain_source=retain_source):
                after = report("4.11.0")
                after["violations"] = []
                for group in groups:
                    entry = outcome("legacy-name", group)
                    if group != "inapplicable":
                        entry["nodes"][0]["target"] = target
                    after[group] = [entry]
                other = outcome("Legacy-name")
                other["nodes"][0]["target"] = nested
                after["passes"].append(other)
                new = catalog("4.11.0", ["Legacy-name"] + (["legacy-name"] if retain_source else []), "fixture-rules-v2")
                result = self.diagnose(before, after, catalog(), new, mapping)
                for finding in result["removed"]:
                    observed = int("passes" in groups and target == nested)
                    self.assertEqual(finding["ruleTargetCoverage"], {
                        "rule": "legacy-name", "target": nested,
                        "status": "observed-target-outcome" if observed else "no-observed-target-outcome",
                        "observedNodes": {"violations": 0, "passes": observed}})
                    self.assertEqual(finding["ruleCoverage"], {
                        "status": ("absent-from-current-catalog" if not retain_source else
                                   "reported-in-current" if groups else "not-reported-in-current"),
                        "outcomes": sorted(groups)})

    def test_mapped_target_observations_are_shared_and_do_not_allocate_leftovers(self):
        nested = ["iframe#checkout", ["checkout-shell", "button.buy"]]
        flat = ["iframe#checkout", "checkout-shell", "button.buy"]
        before = report(targets=[nested, nested, nested, ["#pass-only"], ["#missing"]])
        after = report("4.11.0", "replacement-name", [nested, flat, ["#elsewhere"]])
        passed = outcome("replacement-name")
        node = passed["nodes"][0]
        passed["nodes"] = [dict(deepcopy(node), target=t) for t in (nested, nested, ["#pass-only"], ["#pass-only"])]
        after["passes"] = [passed]
        after["inapplicable"] = [outcome("replacement-name", "inapplicable")]
        old, new = catalog(), catalog("4.11.0", ("replacement-name",), "fixture-rules-v2")
        original = deepcopy((before, after, old, new))
        result = self.diagnose(before, after, old, new)
        self.assertEqual(result["counts"], dict(baseline=5, current=3, renamed=1, unchanged=0, added=2, removed=4))
        nested_leftovers = [f for f in result["removed"] if f["target"] == nested]
        self.assertEqual(len(nested_leftovers), 2)
        for finding in result["removed"]:
            counts = ({"violations": 1, "passes": 2} if finding["target"] == nested else
                      {"violations": 0, "passes": 2} if finding["target"] == ["#pass-only"] else
                      {"violations": 0, "passes": 0})
            self.assertEqual(finding["mappedTargetCoverage"], {
                "rule": "replacement-name", "target": finding["target"],
                "status": "observed-target-outcome" if any(counts.values()) else "no-observed-target-outcome",
                "observedNodes": counts})
            self.assertEqual(finding["mappedRuleCoverage"]["outcomes"], ["inapplicable", "passes", "violations"])
        # One violation already paired as renamed is still one shared native-node
        # observation for each leftover, never two allocated matches or fixes.
        self.assertEqual(nested_leftovers[0]["mappedTargetCoverage"], nested_leftovers[1]["mappedTargetCoverage"])
        ordinary = bridge.diagnose_migration(snapshot(before), snapshot(after), plan())
        self.assertEqual(result["renamed"], ordinary["renamed"])
        self.assertEqual(result["counts"], ordinary["counts"])
        self.assertNotIn("fixed", result)
        self.assertNotIn("gate", result)
        self.assertEqual((before, after, old, new), original)
        after["passes"][0]["nodes"].reverse()
        after["violations"][0]["nodes"].reverse()
        before["violations"][0]["nodes"].reverse()
        replay_inputs = deepcopy((before, after, old, new))
        replay = self.diagnose(before, after, old, new)
        for key in ("removed", "renamed", "added", "counts", "migration", "ruleCatalogs"):
            self.assertEqual(result[key], replay[key])
        # Separate annotations and targets must not share mutable data.
        nested_leftovers[0]["mappedTargetCoverage"]["observedNodes"]["passes"] = 99
        nested_leftovers[0]["mappedTargetCoverage"]["target"][1][0] = "changed"
        self.assertEqual(nested_leftovers[1]["mappedTargetCoverage"]["observedNodes"]["passes"], 2)
        self.assertEqual(nested_leftovers[0]["target"], nested)
        self.assertEqual((before, after, old, new), replay_inputs)

    def test_mapped_target_observations_require_full_target_and_destination_rule(self):
        nested = ["iframe#checkout", ["checkout-shell", "button.buy"]]
        flat = ["iframe#checkout", "checkout-shell", "button.buy"]
        before = report(targets=[nested, nested, nested])
        old = catalog()
        for groups, targets, expected in (
            ((), [], {"violations": 0, "passes": 0}),
            (("inapplicable",), [], {"violations": 0, "passes": 0}),
            (("violations",), [["#elsewhere"]], {"violations": 0, "passes": 0}),
            (("passes",), [flat], {"violations": 0, "passes": 0}),
            (("passes",), [["iframe#checkout", ["button.buy", "checkout-shell"]]], {"violations": 0, "passes": 0}),
            (("passes",), [["iframe#checkout", ["checkout-shell", "button.buy "]]], {"violations": 0, "passes": 0}),
            (("passes",), [nested, nested], {"violations": 0, "passes": 2}),
            (("violations", "passes", "inapplicable"), [nested], {"violations": 1, "passes": 1}),
        ):
            with self.subTest(groups=groups, targets=targets):
                after = report("4.11.0", targets=[])
                after["violations"] = []
                for group in groups:
                    entry = outcome("replacement-name", group)
                    if group != "inapplicable":
                        node = entry["nodes"][0]
                        entry["nodes"] = [dict(deepcopy(node), target=t) for t in targets]
                    after[group] = [entry]
                # Same-target passes under a different rule must never contribute.
                other = outcome("other-rule")
                other["nodes"][0]["target"] = nested
                after["passes"].append(other)
                new = catalog("4.11.0", ("replacement-name", "other-rule"), "fixture-rules-v2")
                result = self.diagnose(before, after, old, new)
                self.assertGreaterEqual(len(result["removed"]), 2)
                for finding in result["removed"]:
                    self.assertEqual(finding["mappedTargetCoverage"], {
                        "rule": "replacement-name", "target": nested,
                        "status": "observed-target-outcome" if any(expected.values()) else "no-observed-target-outcome",
                        "observedNodes": expected})
                    self.assertEqual(finding["mappedRuleCoverage"]["outcomes"], sorted(groups))

    def test_mapped_coverage_preserves_partial_rename_and_duplicate_nested_removals(self):
        nested = ["iframe#checkout", ["checkout-shell", "button.buy"]]
        flat = ["iframe#checkout", "checkout-shell", "button.buy"]
        before = report(targets=[nested, nested, nested, ["#removed"]])
        after = report("4.11.0", "replacement-name", [nested, flat, ["#elsewhere"]])
        old, new = catalog(), catalog("4.11.0", ("replacement-name",), "fixture-rules-v2")
        mapping = plan()
        mapping["renames"][0]["reason"] = "  Caller-reviewed custom rename\nverbatim  "
        original = deepcopy((before, after, old, new, mapping))
        ordinary = bridge.diagnose_migration(snapshot(before), snapshot(after), mapping)
        result = self.diagnose(before, after, old, new, mapping)
        self.assertEqual(result["counts"], dict(baseline=4, current=3, renamed=1, unchanged=0, added=2, removed=3))
        self.assertEqual(result["renamed"], ordinary["renamed"])
        self.assertEqual(result["migration"]["renames"][0]["matchedOccurrences"], 1)
        self.assertEqual(sum(f["target"] == nested for f in result["removed"]), 2)
        self.assertEqual(result["counts"], ordinary["counts"])
        for annotated, native in zip(result["removed"], ordinary["removed"]):
            self.assertEqual(annotated["ruleCoverage"], {"status": "absent-from-current-catalog", "outcomes": []})
            self.assertEqual(annotated["mappedRuleCoverage"], {
                "rule": "replacement-name", "reason": mapping["renames"][0]["reason"],
                "status": "reported-in-current", "outcomes": ["violations"]})
            self.assertEqual({k: v for k, v in annotated.items() if k not in
                              ("ruleCoverage", "ruleTargetCoverage", "mappedRuleCoverage", "mappedTargetCoverage")}, native)
        self.assertNotIn("fixed", result)
        self.assertNotIn("gate", result)
        self.assertEqual((before, after, old, new, mapping), original)
        before["violations"][0]["nodes"].reverse()
        after["violations"][0]["nodes"].reverse()
        replay = self.diagnose(before, after, old, new, mapping)
        for key in ("counts", "renamed", "removed", "added", "migration", "ruleCatalogs"):
            self.assertEqual(result[key], replay[key])

    def test_mapped_destination_outcomes_are_independent_of_original_rule_coverage(self):
        nested = ["iframe#checkout", ["checkout-shell", "button.buy"]]
        before = report(targets=[nested, nested])
        old = catalog()
        for groups in ((), ("violations",), ("passes",), ("inapplicable",),
                       ("violations", "passes", "inapplicable")):
            for retain_source in (False, True):
                with self.subTest(groups=groups, retain_source=retain_source):
                    after = report("4.11.0", targets=[])
                    after["violations"] = []
                    for group in groups:
                        after[group] = [outcome("replacement-name", group)]
                    rules = ["replacement-name"]
                    if retain_source:
                        rules.append("legacy-name")
                        after["passes"].append(outcome("legacy-name"))
                    new = catalog("4.11.0", rules, "fixture-rules-v2")
                    result = self.diagnose(before, after, old, new)
                    self.assertEqual(result["counts"], dict(baseline=2, current=int("violations" in groups),
                        renamed=0, unchanged=0, added=int("violations" in groups), removed=2))
                    self.assertEqual(result["migration"]["renames"][0]["status"], "unmatched")
                    for finding in result["removed"]:
                        self.assertEqual(finding["target"], nested)
                        self.assertEqual(finding["mappedRuleCoverage"], {
                            "rule": "replacement-name", "reason": plan()["renames"][0]["reason"],
                            "status": "reported-in-current" if groups else "not-reported-in-current",
                            "outcomes": sorted(groups)})
                        self.assertEqual(finding["ruleCoverage"], {
                            "status": "reported-in-current" if retain_source else "absent-from-current-catalog",
                            "outcomes": ["passes"] if retain_source else []})

    def test_mapped_coverage_requires_catalogs_and_declared_source_alias(self):
        before, after, old, new = inputs()
        ordinary = bridge.diagnose_migration(snapshot(before), snapshot(after), plan())
        self.assertTrue(all("ruleCoverage" not in f and "ruleTargetCoverage" not in f
                            and "mappedRuleCoverage" not in f and "mappedTargetCoverage" not in f
                            for f in ordinary["removed"]))
        annotated = self.diagnose(before, after, old, new)
        for finding in annotated["removed"]:
            self.assertEqual(finding["ruleTargetCoverage"], {
                "rule": finding["rule"], "target": finding["target"],
                "status": "no-observed-target-outcome", "observedNodes": {"violations": 0, "passes": 0}})
            self.assertEqual("mappedRuleCoverage" in finding, finding["rule"] == "legacy-name")
            self.assertEqual("mappedTargetCoverage" in finding, finding["rule"] == "legacy-name")
        mapping = plan()
        mapping["renames"] = []
        without_alias = self.diagnose(before, after, old, new, mapping)
        self.assertEqual(without_alias["counts"], dict(baseline=5, current=2, renamed=0, unchanged=0, added=2, removed=5))
        self.assertTrue(all("ruleCoverage" in f and "ruleTargetCoverage" in f
                            and "mappedRuleCoverage" not in f and "mappedTargetCoverage" not in f
                            for f in without_alias["removed"]))
        # Original-ID annotations remain the same with or without the declared alias.
        original_coverage = {f["fingerprint"]: f for f in without_alias["removed"]}
        for finding in annotated["removed"]:
            self.assertEqual({k: v for k, v in finding.items() if k not in ("mappedRuleCoverage", "mappedTargetCoverage")},
                             original_coverage[finding["fingerprint"]])
        self.assertEqual(annotated["ruleCatalogs"], without_alias["ruleCatalogs"])

    def test_removed_occurrence_catalog_absence_reported_and_missing_coverage(self):
        before, after, old, new = inputs()
        originals = deepcopy((before, after, old, new))
        result = self.diagnose(before, after, old, new)
        coverage = {f["rule"]: f["ruleCoverage"] for f in result["removed"]}
        self.assertEqual(coverage["retired-rule"], {"status": "absent-from-current-catalog", "outcomes": []})
        self.assertEqual(coverage["legacy-name"], {"status": "absent-from-current-catalog", "outcomes": []})
        self.assertEqual(coverage["still-rule"], {"status": "reported-in-current", "outcomes": ["inapplicable"]})
        self.assertEqual(coverage["skipped-rule"], {"status": "not-reported-in-current", "outcomes": []})
        self.assertEqual(result["ruleCatalogs"]["current"]["notReportedRules"], ["skipped-rule"])
        self.assertEqual(result["ruleCatalogs"]["addedRules"], ["replacement-name"])
        self.assertEqual(result["ruleCatalogs"]["removedRules"], ["legacy-name", "retired-rule"])
        self.assertEqual(result["ruleCatalogs"]["baseline"]["catalog"], old)
        self.assertEqual(result["ruleCatalogs"]["current"]["catalog"], new)
        self.assertEqual((before, after, old, new), originals)
        ordinary = bridge.diagnose_migration(snapshot(before), snapshot(after), plan())
        self.assertEqual(result["counts"], ordinary["counts"])
        self.assertNotIn("ruleCatalogs", ordinary)
        self.assertTrue(all("ruleCoverage" not in f for f in ordinary["removed"]))
        self.assertNotIn("fixed", result)
        self.assertNotIn("gate", result)

    def test_rule_can_have_pass_and_violation_outcomes_and_duplicates_stay_occurrences(self):
        before, after, old, new = inputs()
        after["violations"].append(outcome("still-rule"))
        after["passes"] = [outcome("still-rule")]
        before["violations"][-1]["nodes"].append(deepcopy(before["violations"][-1]["nodes"][0]))
        result = self.diagnose(before, after, old, new)
        removed = [f for f in result["removed"] if f["rule"] == "still-rule"]
        # Same rule on another target: coverage is page/rule evidence, not target proof.
        self.assertEqual(removed, [])  # exact same target pairs unchanged
        before["violations"][2]["nodes"][0]["target"] = ["#disappeared"]
        result = self.diagnose(before, after, old, new)
        removed = [f for f in result["removed"] if f["rule"] == "still-rule"]
        self.assertEqual(removed[0]["ruleCoverage"]["outcomes"], ["inapplicable", "passes", "violations"])
        self.assertEqual(len([f for f in result["removed"] if f["rule"] == "skipped-rule"]), 2)
        after["passes"].reverse()
        new["rules"].reverse()
        replay = self.diagnose(before, after, old, new)
        self.assertEqual(result["ruleCatalogs"]["current"]["reportedRules"], replay["ruleCatalogs"]["current"]["reportedRules"])

    def test_empty_catalog_is_valid_only_for_empty_report_and_undeclared_aliases(self):
        before = report()
        after = report("4.11.0")
        before["violations"] = after["violations"] = []
        mapping = plan()
        mapping["renames"] = []
        result = self.diagnose(before, after, catalog(rules=()), catalog("4.11.0", (), "fixture-rules-v2"), mapping)
        self.assertEqual(result["ruleCatalogs"]["current"]["reportedRules"], [])
        self.assertEqual(result["ruleCatalogs"]["addedRules"], [])

    def test_catalog_schema_and_provenance_negative_contracts(self):
        before, after, old, new = inputs()
        malformed = [None, [], {}, {**new, "schemaVersion": True}, {**new, "extra": 1}]
        for field, value in (("kind", "other"), ("format", "pa11y-ci"), ("format", []),
                             ("inputVersion", "4.10.3"), ("inputVersion", "4.99.0"),
                             ("ruleSet", "wrong"), ("ruleSet", " "), ("rules", {}),
                             ("export", {"api": "axe.getRules", "tags": ["wcag2a"]}),
                             ("export", {"api": "other", "tags": None}), ("export", {})):
            malformed.append({**new, field: value})
        malformed.append({**new, "rules": new["rules"] * 2})
        for mutation in ({"ruleId": " "}, {"helpUrl": "javascript:bad"}, {"tags": "tag"},
                         {"tags": [None]}, {"actIds": [2]}, {"help": ""}, {"extra": 1}):
            malformed.append({**new, "rules": [{**new["rules"][0], **mutation}, *new["rules"][1:]]})
        for value in malformed:
            with self.subTest(catalog=value), self.assertRaises(bridge.BridgeError):
                self.diagnose(before, after, old, value)
        with self.assertRaises(bridge.BridgeError):
            self.diagnose(before, after, None, new)
        with self.assertRaises(bridge.BridgeError):
            self.diagnose(before, after, old, None)
        # Optional actIds is truly optional; metadata is retained verbatim.
        del new["rules"][0]["actIds"]
        self.diagnose(before, after, old, new)

    def test_every_observed_group_id_and_rename_endpoint_must_be_cataloged(self):
        before, after, old, new = inputs()
        for group in ("violations", "passes", "inapplicable"):
            changed = deepcopy(after)
            changed[group].append(outcome("uncataloged", group))
            with self.subTest(group=group), self.assertRaisesRegex(bridge.BridgeError, "catalog"):
                self.diagnose(before, changed, old, new)
        for side in ("from", "to"):
            mapping = plan()
            mapping["renames"][0][side] = "uncataloged"
            with self.subTest(endpoint=side), self.assertRaisesRegex(bridge.BridgeError, "catalog"):
                self.diagnose(before, after, old, new, mapping)
        # A catalog with the wrong baseline version/label is rejected too.
        for field in ("inputVersion", "ruleSet"):
            changed = {**old, field: new[field]}
            with self.subTest(field=field), self.assertRaises(bridge.BridgeError):
                self.diagnose(before, after, changed, new)

    def test_malformed_coverage_is_not_evidence_of_execution(self):
        before, after, old, new = inputs()
        for group in ("passes", "inapplicable"):
            for entry in (None, {}, {"id": "still-rule"}, {**outcome("still-rule", group), "nodes": {}},
                          {**outcome("still-rule", group), "tags": None}):
                changed = deepcopy(after)
                changed[group] = [entry]
                with self.subTest(group=group, entry=entry), self.assertRaises(bridge.BridgeError):
                    self.diagnose(before, changed, old, new)
        for group in ("passes", "inapplicable", "violations"):
            changed = deepcopy(after)
            changed[group] = [outcome("still-rule", group)] * 2
            with self.subTest(group=group), self.assertRaises(bridge.BridgeError):
                self.diagnose(before, changed, old, new)
        changed = deepcopy(after)
        changed["passes"] = [outcome("still-rule")]
        changed["passes"][0]["nodes"] = []
        with self.assertRaises(bridge.BridgeError):
            self.diagnose(before, changed, old, new)

    def test_pa11y_catalogs_are_explicitly_unsupported(self):
        native = json.loads((ROOT / "examples/a11y/pa11y.json").read_text())
        scan = bridge.make_baseline(native, "pa11y-ci", input_version="4.0.0")
        with self.assertRaisesRegex(bridge.BridgeError, "axe"):
            bridge.diagnose_migration(scan, scan, plan("pa11y-ci", "4.0.0", "4.0.0", "pa11y-ci/htmlcs"),
                                     baseline_catalog=catalog(), current_catalog=catalog())


class CatalogCliTests(unittest.TestCase):
    def test_mapped_coverage_cli_and_invalid_provenance_or_endpoints_do_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            before, after, old, new = inputs()
            mapping = plan()
            documents = {"baseline": snapshot(before), "current": after, "map": mapping, "old": old, "new": new}
            for name, value in documents.items():
                (path / f"{name}.json").write_text(json.dumps(value))
            out = path / "out.json"
            command = [sys.executable, "-m", "qawrench.a11y", "migration", "--format", "axe",
                       "--url", "https://fixture.test", "--baseline", str(path / "baseline.json"),
                       str(path / "current.json"), "--rule-map", str(path / "map.json"),
                       "--baseline-catalog", str(path / "old.json"), "--current-catalog", str(path / "new.json"),
                       "--out", str(out)]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            diagnostic = json.loads(out.read_text())
            removed = next(f for f in diagnostic["removed"] if f["rule"] == "legacy-name")
            self.assertEqual(removed["ruleTargetCoverage"], {
                "rule": "legacy-name", "target": ["#removed"],
                "status": "no-observed-target-outcome", "observedNodes": {"violations": 0, "passes": 0}})
            self.assertEqual(removed["mappedRuleCoverage"], {
                "rule": "replacement-name", "reason": mapping["renames"][0]["reason"],
                "status": "reported-in-current", "outcomes": ["violations"]})
            self.assertEqual(removed["mappedTargetCoverage"], {
                "rule": "replacement-name", "target": ["#removed"],
                "status": "no-observed-target-outcome", "observedNodes": {"violations": 0, "passes": 0}})
            out.unlink()
            bad_inputs = [("new", {**new, "ruleSet": "wrong"}),
                          ("old", {**old, "inputVersion": "4.11.0"})]
            for endpoint in ("from", "to"):
                bad_map = deepcopy(mapping)
                bad_map["renames"][0][endpoint] = "unknown-rule"
                bad_inputs.append(("map", bad_map))
            for target in ([], [["single-shadow-selector"]], ["iframe#checkout", ["checkout-shell", 1]]):
                malformed = deepcopy(after)
                passed = outcome("replacement-name")
                passed["nodes"][0]["target"] = target
                malformed["passes"] = [passed]
                bad_inputs.append(("current", malformed))
            for node in (None, {}, {"target": ["#removed", []]}):
                malformed = deepcopy(after)
                passed = outcome("replacement-name")
                passed["nodes"] = [node]
                malformed["passes"] = [passed]
                bad_inputs.append(("current", malformed))
            for name, value in bad_inputs:
                with self.subTest(name=name, value=value):
                    (path / f"{name}.json").write_text(json.dumps(value))
                    for existing_output in (False, True):
                        if existing_output:
                            out.write_text("existing output stays intact")
                        rejected = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
                        self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
                        self.assertNotIn("Traceback", rejected.stderr)
                        if existing_output:
                            self.assertEqual(out.read_text(), "existing output stays intact")
                            out.unlink()
                        else:
                            self.assertFalse(out.exists())
                    (path / f"{name}.json").write_text(json.dumps(documents[name]))

    def test_paired_inputs_dry_run_and_catalog_hardlink_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            before, after, old, new = inputs()
            for name, value in (("baseline", snapshot(before)), ("current", after), ("map", plan()), ("old", old), ("new", new)):
                (path / f"{name}.json").write_text(json.dumps(value))
            command = [sys.executable, "-m", "qawrench.a11y", "migration", "--format", "axe",
                       "--url", "https://fixture.test", "--baseline", str(path / "baseline.json"),
                       str(path / "current.json"), "--rule-map", str(path / "map.json")]
            catalogs = ["--baseline-catalog", str(path / "old.json"), "--current-catalog", str(path / "new.json")]
            out = path / "out.json"

            def run(*args, expected=0):
                result = subprocess.run([*command, *args], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                return result

            run(*catalogs, "--out", str(out), "--dry-run")
            self.assertFalse(out.exists())
            run(*catalogs, "--out", str(out))
            self.assertEqual(json.loads(out.read_text())["ruleCatalogs"]["removedRules"], ["legacy-name", "retired-rule"])
            out.unlink()
            for option in (catalogs[:2], catalogs[2:]):
                run(*option, "--out", str(out), expected=2)
                self.assertFalse(out.exists())
            for source in (path / "old.json", path / "new.json"):
                alias = path / "alias.json"
                alias.hardlink_to(source)
                original = source.read_bytes()
                for dest in (source, alias):
                    run(*catalogs, "--out", str(dest), expected=2)
                    self.assertEqual(source.read_bytes(), original)
                alias.unlink()
            for content in ("null", '{"rules":[],"rules":[]}', '{"schemaVersion":NaN}', json.dumps({**new, "ruleSet": "wrong"})):
                (path / "new.json").write_text(content)
                run(*catalogs, "--out", str(out), expected=2)
                self.assertFalse(out.exists())
            # An explicit empty pathname is also an invalid input, not omission.
            run("--baseline-catalog", "", "--current-catalog", "", "--out", str(out), expected=2)
            self.assertFalse(out.exists())
            # Explicit JSON null files are invalid, not omitted optional inputs.
            (path / "old.json").write_text("null")
            (path / "new.json").write_text("null")
            run(*catalogs, "--out", str(out), expected=2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
