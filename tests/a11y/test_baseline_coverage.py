"""Baseline observations for added occurrences are shared evidence, not regressions."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import qawrench.a11y as bridge
from .test_catalog import catalog, outcome
from .test_migration import ROOT, plan, report, snapshot
from . import test_migration_sarif as sarif_tests

NESTED = ["iframe#checkout", ["checkout-shell", "button.buy"]]
FIELDS = ("baselineRuleCoverage", "baselineRuleTargetCoverage")


def coverage_inputs():
    before = report(targets=[["#renamed"], ["#leftover"]])
    after = report("4.11.0", "replacement-name", [["#renamed"], ["#added"]])
    before["violations"].append(report(rule="same-rule", targets=[NESTED])["violations"][0])
    after["violations"].append(report(rule="same-rule", targets=[NESTED, NESTED, NESTED,
                                                             ["#pass-only"], ["#missing"]])["violations"][0])
    passed = outcome("same-rule")
    node = passed["nodes"][0]
    passed["nodes"] = [dict(deepcopy(node), target=deepcopy(t))
                       for t in (NESTED, NESTED, ["#pass-only"], ["#pass-only"])]
    before["passes"] = [passed]
    before["inapplicable"] = [outcome("same-rule", "inapplicable")]
    return (before, after, catalog(rules=("legacy-name", "same-rule")),
            catalog("4.11.0", ("replacement-name", "same-rule"), "fixture-rules-v2"), plan())


def diagnose(values, catalogs=True):
    before, after, old, new, mapping = values
    return bridge.diagnose_migration(snapshot(before), snapshot(after), mapping,
        **(dict(baseline_catalog=old, current_catalog=new) if catalogs else {}))


class BaselineCoverageTests(unittest.TestCase):
    def test_baseline_added_observations_share_already_unchanged_nodes(self):
        values = coverage_inputs()
        original = deepcopy(values)
        result = diagnose(values)
        self.assertEqual(result["counts"], dict(baseline=3, current=7, unchanged=1, renamed=1, added=5, removed=1))
        for finding in result["added"]:
            same = finding["rule"] == "same-rule"
            self.assertEqual(finding["baselineRuleCoverage"], {
                "status": "reported-in-baseline" if same else "absent-from-baseline-catalog",
                "outcomes": ["inapplicable", "passes", "violations"] if same else []})
            counts = ({"violations": 1, "passes": 2} if finding["target"] == NESTED else
                      {"violations": 0, "passes": 2} if finding["target"] == ["#pass-only"] else
                      {"violations": 0, "passes": 0})
            self.assertEqual(finding["baselineRuleTargetCoverage"], {
                "rule": finding["rule"], "target": finding["target"],
                "status": "observed-target-outcome" if any(counts.values()) else "no-observed-target-outcome",
                "observedNodes": counts})
        ordinary = diagnose(values, catalogs=False)
        for key in ("counts", "unchanged", "renamed", "sources", "migration", "pages"):
            self.assertEqual(result[key], ordinary[key])
        self.assertEqual([{k: v for k, v in f.items() if k not in FIELDS} for f in result["added"]], ordinary["added"])
        for key in ("removed", "renamed", "unchanged"):
            self.assertTrue(all(field not in f for f in result[key] for field in FIELDS))
        self.assertEqual(values, original)
        nested = [f for f in result["added"] if f["target"] == NESTED]
        self.assertEqual(len(nested), 2)
        nested[0]["baselineRuleTargetCoverage"]["target"][1][0] = "mutated"
        nested[0]["baselineRuleTargetCoverage"]["observedNodes"]["passes"] = 99
        nested[0]["baselineRuleCoverage"]["outcomes"].append("mutated")
        self.assertEqual(nested[1]["baselineRuleTargetCoverage"]["observedNodes"]["passes"], 2)
        self.assertEqual(nested[1]["baselineRuleCoverage"]["outcomes"], ["inapplicable", "passes", "violations"])
        self.assertEqual(nested[0]["target"], NESTED)
        self.assertEqual(values, original)

    def test_baseline_page_status_and_literal_rule_full_target_boundaries(self):
        cases = [
            ((), NESTED, "same-rule", True),
            ((), NESTED, "same-rule", False),
            (("inapplicable",), NESTED, "same-rule", True),
            (("violations", "passes"), ["#elsewhere"], "same-rule", True),
            (("passes",), ["iframe#checkout", "checkout-shell", "button.buy"], "same-rule", True),
            (("passes",), [["checkout-shell", "button.buy"], "iframe#checkout"], "same-rule", True),
            (("passes",), ["iframe#checkout", ["button.buy", "checkout-shell"]], "same-rule", True),
            (("passes",), ["iframe#checkout ", ["checkout-shell", "button.buy"]], "same-rule", True),
            (("violations", "passes"), NESTED, "Same-rule", True),
        ]
        for groups, target, rule, present in cases:
            with self.subTest(groups=groups, target=target, rule=rule, present=present):
                before = report(); before["violations"] = []
                for group in groups:
                    entry = outcome(rule, group)
                    if group != "inapplicable":
                        entry["nodes"][0]["target"] = target
                    before[group] = [entry]
                after = report("4.11.0", "same-rule", [NESTED])
                ids = sorted(({rule} if groups else set()) | ({"same-rule"} if present else set()))
                values = (before, after, catalog(rules=ids),
                          catalog("4.11.0", ("same-rule",), "fixture-rules-v2"), {**plan(), "renames": []})
                finding = diagnose(values)["added"][0]
                expected_groups = sorted(groups) if rule == "same-rule" else []
                self.assertEqual(finding["baselineRuleCoverage"], {
                    "status": "absent-from-baseline-catalog" if not present else
                              "reported-in-baseline" if expected_groups else "not-reported-in-baseline",
                    "outcomes": expected_groups})
                self.assertEqual(finding["baselineRuleTargetCoverage"], {
                    "rule": "same-rule", "target": NESTED, "status": "no-observed-target-outcome",
                    "observedNodes": {"violations": 0, "passes": 0}})

    def test_added_sarif_notes_retain_baseline_fields_without_reverse_aliases(self):
        values = coverage_inputs()
        diagnostics = diagnose(values)
        original = deepcopy(diagnostics)
        sarif = bridge.migration_to_sarif(diagnostics)
        # Reuse the structural/fingerprint contract; no new SARIF semantics.
        run = sarif_tests.MigrationSarifTests().validate(sarif)
        added = [r for r in run["results"] if r["properties"]["observation"] == "added"]
        self.assertEqual(len(added), 5)
        for result in added:
            props = result["properties"]
            finding = next(f for f in diagnostics["added"] if f["fingerprint"] == props["fingerprint"])
            for field in FIELDS:
                self.assertEqual(props[field], finding[field])
        replacement = next(r["properties"] for r in added if r["properties"]["rule"] == "replacement-name")
        self.assertEqual(replacement["baselineRuleCoverage"], {"status": "absent-from-baseline-catalog", "outcomes": []})
        self.assertEqual(replacement["baselineRuleTargetCoverage"]["observedNodes"], {"violations": 0, "passes": 0})
        # A cataloged alias destination can have baseline passes, but no reverse source observation.
        before, after, old, new, mapping = deepcopy(values)
        old["rules"].extend(catalog(rules=("replacement-name",))["rules"])
        before["passes"].append(outcome("replacement-name"))
        literal = diagnose((before, after, old, new, mapping))["added"]
        literal = next(f for f in literal if f["rule"] == "replacement-name")
        self.assertEqual(literal["baselineRuleCoverage"], {"status": "reported-in-baseline", "outcomes": ["passes"]})
        self.assertEqual(literal["baselineRuleTargetCoverage"]["observedNodes"], {"violations": 0, "passes": 0})
        for r in added:
            r["properties"]["baselineRuleTargetCoverage"]["target"].append("mutated")
        self.assertEqual(diagnostics, original)
        ordinary = diagnose(values, catalogs=False)
        plain = bridge.migration_to_sarif(ordinary)
        self.assertTrue(all(field not in r["properties"] for r in plain["runs"][0]["results"] for field in FIELDS))
        # Native order retained in sources, deterministic observations under node reorder.
        before, after, old, new, mapping = deepcopy(values)
        before["passes"][0]["nodes"].reverse()
        after["violations"][1]["nodes"].reverse()
        replay = diagnose((before, after, old, new, mapping))
        for key in ("added", "removed", "unchanged", "renamed", "counts", "migration", "ruleCatalogs"):
            self.assertEqual(replay[key], diagnostics[key])
        self.assertEqual(replay["sources"]["baseline"]["report"], before)


class BaselineCoverageCliTests(unittest.TestCase):
    def test_baseline_invalid_inputs_fail_before_json_or_sarif_writes(self):
        before, after, old, new, mapping = coverage_inputs()
        documents = dict(baseline=snapshot(before), current=after, old=old, new=new, map=mapping)
        mutations = [
            ("baseline", lambda d: d["source"]["report"]["passes"][0]["nodes"][0].update(target=[])),
            ("baseline", lambda d: d["source"]["report"]["inapplicable"][0].update(nodes=[{}])),
            ("baseline", lambda d: d["source"]["report"]["passes"][0].update(id="uncataloged")),
            ("old", lambda d: d.update(ruleSet="wrong-provenance")),
            ("old", lambda d: d.update(export={"api": "axe.getRules", "tags": []})),
            ("map", lambda d: d["renames"][0].update({"from": "unknown-endpoint"})),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            files = {k: path / (k + '.json') for k in documents}
            for k, value in documents.items():
                files[k].write_text(json.dumps(value))
            command = [sys.executable, '-m', 'qawrench.a11y', 'migration', '--format', 'axe',
                       '--url', 'https://fixture.test', '--baseline', str(files['baseline']),
                       str(files['current']), '--rule-map', str(files['map']),
                       '--baseline-catalog', str(files['old']), '--current-catalog', str(files['new'])]
            for key, mutate in mutations:
                changed = deepcopy(documents[key]); mutate(changed)
                files[key].write_text(json.dumps(changed))
                for fmt in ('json', 'sarif'):
                    for existing in (False, True):
                        with self.subTest(key=key, fmt=fmt, existing=existing, mutate=mutate):
                            out = path / ('output.' + fmt)
                            if existing:
                                out.write_bytes(b'keep-existing-output\n')
                            result = subprocess.run([*command, '--output-format', fmt, '--out', str(out)],
                                                    cwd=ROOT, capture_output=True, text=True)
                            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                            self.assertNotIn('Traceback', result.stderr)
                            if existing:
                                self.assertEqual(out.read_bytes(), b'keep-existing-output\n')
                                out.unlink()
                            else:
                                self.assertFalse(out.exists())
                files[key].write_text(json.dumps(documents[key]))


if __name__ == "__main__":
    unittest.main()
