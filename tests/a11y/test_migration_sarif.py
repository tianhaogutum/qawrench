"""Migration observations exported as notes, never SARIF absent/fixed results."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft7Validator, FormatChecker, ValidationError

import qawrench.a11y as bridge
from qawrench.a11y.core import canonical
from .test_catalog import catalog, inputs, outcome
from .test_migration import ROOT, plan, report, snapshot

WARNING = "Disappearances do not establish fixes or target retesting."


def diagnose(before, after, mapping=None, old=None, new=None):
    return bridge.diagnose_migration(snapshot(before), snapshot(after), mapping or plan(),
                                    baseline_catalog=old, current_catalog=new)


def multipage_inputs():
    """Native-valid literal URLs, including IRI text beyond SARIF's URI format."""
    pages = ['HTTPS://Fixture.TEST:443/購物?b=2&a=%2f&say="購物"#區',
             'https://fixture.test/購物?a=%2f&b=2&say="購物"#區',
             'https://fixture.test/購物?b=2&a=%2F&say="購物"#區',
             'https://fixture.test/購物?b=2&a=%2f&say="購物"#別']
    template = json.loads((ROOT / "examples/a11y/pa11y.json").read_text())["results"]["https://fixture.test/"]
    reports = []
    for rule, leftover in (("legacy-name", "#removed"), ("replacement-name", "#added")):
        results = {}
        for page in pages:
            issues = [dict(deepcopy(template[0]), code=rule, selector=target)
                      for target in (" button#購物 ", " button#購物 ", leftover)]
            issues.append(dict(deepcopy(template[1]), code=rule, selector=""))
            results[page] = issues
        reports.append({"total": len(pages), "passes": 0, "errors": 4 * len(pages), "results": results})
    mapping = plan("pa11y-ci", "4.0.0", "4.0.0", "pa11y-ci/htmlcs")
    mapping["renames"].append({**mapping["renames"][0], "engine": "pa11y-ci/axe"})
    return *reports, mapping


class MigrationSarifTests(unittest.TestCase):
    def test_uri_checker_is_registered_and_rejects_invalid_locations(self):
        checker = FormatChecker()
        self.assertIn("uri", checker.checkers, "Install the test extra (rfc3986-validator); URI checks must not silently skip")
        validator = Draft7Validator({"type": "string", "format": "uri"}, format_checker=checker)
        for value in ('https://fixture.test/購物', 'https://fixture.test/?say="yes"',
                      'https://fixture.test/%xx', '/relative', 'https://fixture.test/#one#two'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validator.validate(value)
        validator.validate('https://fixture.test/%E8%B3%BC?b=2&a=%2f#one%23two')

    def test_locations_escape_uri_characters_without_rewriting_native_pages(self):
        before, after, mapping = multipage_inputs()
        extra = {
            'https://fixture.test/a[1]|<>^`{}?q=[x]#one#two':
                'https://fixture.test/a%5B1%5D%7C%3C%3E%5E%60%7B%7D?q=%5Bx%5D#one%23two',
            "http://[2001:db8::1]:8080/a:@!$&'()*+,;=~-._/%2f?b=2&a=%2F&x=/?:@!$'()*+,;=#/?:@!$&'()*+,;=":
                "http://[2001:db8::1]:8080/a:@!$&'()*+,;=~-._/%2f?b=2&a=%2F&x=/?:@!$'()*+,;=#/?:@!$&'()*+,;=",
            'https://fixture.test/%E8%B3%BC%E7%89%A9?b=2&a=%2f&say=%22ok%22#%E5%8D%80':
                'https://fixture.test/%E8%B3%BC%E7%89%A9?b=2&a=%2f&say=%22ok%22#%E5%8D%80',
        }
        for native in (before, after):
            template = deepcopy(next(iter(native["results"].values())))
            native["results"].update({page: deepcopy(template) for page in extra})
            native.update(total=len(native["results"]), errors=4 * len(native["results"]))
        baseline, current = [bridge.make_baseline(r, "pa11y-ci", input_version="4.0.0") for r in (before, after)]
        diagnostics = bridge.diagnose_migration(baseline, current, mapping)
        sarif, identities = self.assert_native_page_clauses(diagnostics)
        expected = dict(extra)
        for page in baseline["pages"]:
            if page not in expected:
                expected[page] = page.replace('購物', '%E8%B3%BC%E7%89%A9').replace('"', '%22').replace('區', '%E5%8D%80').replace('別', '%E5%88%A5')
        self.assertEqual(len(expected), 7)
        for pages in identities.values():
            self.assertEqual(set(pages), set(expected))
            for page, notes in pages.items():
                for note in notes:
                    self.assertEqual(note["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], expected[page])
                if len(notes) > 1:
                    self.assertEqual([n["properties"]["occurrence"] for n in notes], [1, 2])
        self.validate(sarif)
        # Native properties accept literal HTTP(S) identity text; only locations
        # carry URI-format semantics. A literal location must fail that contract.
        invalid = deepcopy(sarif)
        invalid["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = 'https://fixture.test/購物?x="y"'
        schema = json.loads((ROOT / "src/qawrench/a11y/schemas/migration-sarif-output.schema.json").read_text())
        errors = list(Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(invalid))
        self.assertEqual(len(errors), 1)
        self.assertEqual(list(errors[0].absolute_path)[-1], "uri")
        replay = deepcopy(diagnostics)
        for status in ("added", "removed", "renamed"):
            replay[status].reverse()
        self.assertEqual(sarif, bridge.migration_to_sarif(replay))

    def assert_native_page_clauses(self, diagnostics):
        original = deepcopy(diagnostics)
        # The serializer must use retained native values, without normalizing again.
        with patch("qawrench.a11y.core.normalize_url", side_effect=AssertionError("re-normalized page")):
            sarif = bridge.migration_to_sarif(diagnostics)
        by_identity = {}
        for result in sarif["runs"][0]["results"]:
            props, message = result["properties"], result["message"]["text"]
            status = props["observation"]
            sides = (("Baseline", props["baseline"]), ("Current", props["current"])) if status == "renamed" else (
                ("Baseline" if status == "removed" else "Current", props),)
            self.assertEqual(message.count(" identity:"), len(sides))
            for label, finding in sides:
                values = [json.dumps(finding[k], ensure_ascii=False, separators=(",", ":"))
                          for k in ("engine", "rule", "target", "page")]
                self.assertIn(f'{label} identity: engine={values[0]}, rule={values[1]}, target={values[2]}, page={values[3]}.', message)
            native = sides[0][1]
            by_identity.setdefault((status, native["engine"], canonical(native["target"])), {}).setdefault(native["page"], []).append(result)
        self.assertEqual(diagnostics, original)
        return sarif, by_identity

    def test_native_pages_distinguish_same_selectors_and_repeated_pairs(self):
        before, after, mapping = multipage_inputs()
        baseline, current = [bridge.make_baseline(r, "pa11y-ci", input_version="4.0.0") for r in (before, after)]
        diagnostics = bridge.diagnose_migration(baseline, current, mapping)
        self.assertEqual(diagnostics["counts"], dict(baseline=16, current=16, unchanged=0, renamed=12, added=4, removed=4))
        expected_pages = [p.replace("HTTPS://Fixture.TEST:443/", "https://fixture.test/") for p in before["results"]]
        self.assertEqual(baseline["pages"], sorted(expected_pages))
        sarif, identities = self.assert_native_page_clauses(diagnostics)
        for pages in identities.values():
            self.assertEqual(set(pages), set(expected_pages))
            self.assertEqual(len({notes[0]["message"]["text"] for notes in pages.values()}), 4)
            for notes in pages.values():
                if len(notes) > 1:
                    self.assertEqual([n["properties"]["occurrence"] for n in notes], [1, 2])
                    self.assertEqual(notes[0]["message"], notes[1]["message"])
                    self.assertEqual(notes[0]["partialFingerprints"], notes[1]["partialFingerprints"])
        # Reordering diagnostics must retain native result ordering and all text.
        replay = deepcopy(diagnostics)
        for status in ("added", "removed", "renamed"):
            replay[status].reverse()
        self.assertEqual(sarif, bridge.migration_to_sarif(replay))

    def test_native_page_ignores_fallback_and_catalog_context(self):
        before, after, old, new = inputs()
        raw_page = "HTTPS://Fixture.TEST:443/checkout?z=%2f&a=2#details"
        for r in (before, after):
            r["url"] = raw_page
        baseline, current = [bridge.make_baseline(r, "axe", url="https://fallback.invalid/unused") for r in (before, after)]
        for catalogs in (False, True):
            with self.subTest(catalogs=catalogs):
                diagnostics = bridge.diagnose_migration(baseline, current, plan(),
                    baseline_catalog=old if catalogs else None, current_catalog=new if catalogs else None)
                sarif, _ = self.assert_native_page_clauses(diagnostics)
                self.validate(sarif)
                for result in sarif["runs"][0]["results"]:
                    self.assertNotIn("fallback.invalid", result["message"]["text"])
                    self.assertNotIn('page="https://fixture.test/help"', result["message"]["text"])
                    self.assertIn('page="https://fixture.test/checkout?z=%2f&a=2#details"', result["message"]["text"])

    def test_rename_rationale_is_literal_and_repeated_for_each_explicit_pair(self):
        reason = '  Caller says "購物" \\ rename\n\tkeep spacing\r  '
        mapping = plan()
        mapping["renames"][0]["reason"] = reason
        mapping["renames"].append({"engine": "axe", "from": "unused-old", "to": "unused-new",
                                   "reason": "Unmatched reason must not leak"})
        nested = [" iframe#購物 ", ["checkout-shell", " button.buy "]]
        diagnostics = diagnose(report(targets=[nested, nested, ["#removed"]]),
                               report("4.11.0", "replacement-name", [nested, nested, ["#added"]]), mapping)
        original = deepcopy(diagnostics)
        run = self.validate(bridge.migration_to_sarif(diagnostics))
        renamed = [r for r in run["results"] if r["properties"]["observation"] == "renamed"]
        self.assertEqual([r["properties"]["occurrence"] for r in renamed], [1, 2])
        clause = 'Caller-declared rename rationale: ' + json.dumps(reason, ensure_ascii=False) + '.'
        for result in run["results"]:
            message = result["message"]["text"]
            if result in renamed:
                self.assertIn(clause, message)
                self.assertEqual(message.count("rationale:"), 1)
            else:
                self.assertNotIn("rationale:", message)
            self.assertNotIn("Unmatched reason", message)
        self.assertEqual(diagnostics, original)

    def test_removed_rationale_requires_existing_mapped_page_reason(self):
        before, after, old, new = inputs()
        mapping = plan()
        reason = ' \tMapped "規則" \\ reason\n '
        mapping["renames"][0]["reason"] = reason
        for catalogs in (False, True):
            with self.subTest(catalogs=catalogs):
                diagnostics = diagnose(before, after, mapping, old if catalogs else None, new if catalogs else None)
                # Formatter boundary: the audit is not the source for removed
                # notes; their existing mapped page annotation is authoritative.
                diagnostics["migration"]["renames"][0]["reason"] = "Audit-only reason"
                original = deepcopy(diagnostics)
                run = self.validate(bridge.migration_to_sarif(diagnostics))
                for result in run["results"]:
                    props, message = result["properties"], result["message"]["text"]
                    if props["observation"] == "renamed":
                        self.assertIn('Caller-declared rename rationale: "Audit-only reason".', message)
                    elif "mappedRuleCoverage" in props:
                        self.assertIn('Caller-declared mapped-rule rationale: ' + json.dumps(reason, ensure_ascii=False) + '.', message)
                        self.assertNotIn("Audit-only reason", message)
                    else:
                        self.assertNotIn("rationale:", message)
                self.assertEqual(diagnostics, original)

    def test_rename_rationale_matches_full_namespace_from_to_audit_key(self):
        before = json.loads((ROOT / "examples/a11y/pa11y.json").read_text())
        for issue in before["results"]["https://fixture.test/"]:
            issue.update(code="legacy-name", selector="")
        after = deepcopy(before)
        for issue in after["results"]["https://fixture.test/"]:
            issue["code"] = "replacement-name"
        mapping = plan("pa11y-ci", "4.0.0", "4.0.0", "pa11y-ci/htmlcs")
        mapping["renames"][0]["reason"] = "HTMLCS reason"
        mapping["renames"].append({"engine": "pa11y-ci/axe", "from": "legacy-name", "to": "replacement-name",
                                   "reason": "axe reason"})
        baseline, current = [bridge.make_baseline(r, "pa11y-ci", input_version="4.0.0") for r in (before, after)]
        diagnostics = bridge.diagnose_migration(baseline, current, mapping)
        run = self.validate(bridge.migration_to_sarif(diagnostics))
        self.assertEqual(len(run["results"]), 2)
        for result in run["results"]:
            engine = result["properties"]["baseline"]["engine"]
            reason = "HTMLCS reason" if engine.endswith("htmlcs") else "axe reason"
            self.assertIn('Caller-declared rename rationale: ' + json.dumps(reason) + '.', result["message"]["text"])
        # These omissions exercise only the serializer boundary, not acceptance
        # of caller-edited diagnostics by diagnose_migration.
        for field, value in (("engine", "axe"), ("from", "other-old"), ("to", "other-new"), ("reason", None)):
            with self.subTest(field=field):
                edited = deepcopy(diagnostics)
                for audit in edited["migration"]["renames"]:
                    if value is None:
                        del audit[field]
                    else:
                        audit[field] = value
                for result in bridge.migration_to_sarif(edited)["runs"][0]["results"]:
                    self.assertNotIn("rationale:", result["message"]["text"])

    def test_missing_explicit_rationale_data_is_omitted_without_fallback(self):
        before, after, old, new = inputs()
        diagnostics = diagnose(before, after, old=old, new=new)
        for audit in diagnostics["migration"]["renames"]:
            audit["reason"] = "Must not backfill mapped reason"
        for finding in diagnostics["removed"]:
            if "mappedRuleCoverage" in finding:
                del finding["mappedRuleCoverage"]["reason"]
        diagnostics["migration"]["renames"] = []
        for result in self.validate(bridge.migration_to_sarif(diagnostics))["results"]:
            self.assertNotIn("rationale:", result["message"]["text"])

    def test_messages_identify_both_native_sides_without_normalizing_targets(self):
        targets = [[" iframe#購物 ", ["checkout-shell", " button.buy "]],
                   [" iframe#購物 ", "checkout-shell", " button.buy "],
                   [" button.buy ", "checkout-shell", " iframe#購物 "], ["#removed"]]
        mapping = plan()
        mapping["renames"][0].update({"from": 'legacy-規則"', "to": "replacement-規則"})
        diagnostics = diagnose(report(rule='legacy-規則"', targets=targets),
                               report("4.11.0", "replacement-規則", targets[:3] + [[" #added "]]), mapping)
        original = deepcopy(diagnostics)
        run = self.validate(bridge.migration_to_sarif(diagnostics))
        self.assertEqual(len(run["results"]), 5)
        for result in run["results"]:
            props, message = result["properties"], result["message"]["text"]
            sides = (("Baseline", props["baseline"]), ("Current", props["current"])) if props["observation"] == "renamed" else (
                ("Baseline" if props["observation"] == "removed" else "Current", props),)
            for label, finding in sides:
                values = [json.dumps(finding[k], ensure_ascii=False, separators=(",", ":"))
                          for k in ("engine", "rule", "target", "page")]
                self.assertIn(f'{label} identity: engine={values[0]}, rule={values[1]}, target={values[2]}, page={values[3]}.', message)
            self.assertNotIn("page observation:", message)
            self.assertNotIn("target observation:", message)
        self.assertEqual(diagnostics, original)

    def test_messages_label_independent_existing_page_and_target_observations(self):
        before, after, old, new = inputs()
        before["passes"] = [outcome("replacement-name")]
        before["passes"][0]["nodes"][0]["target"] = ["#added"]
        old["rules"].extend(catalog(rules=("replacement-name",))["rules"])
        after["passes"] = [outcome("replacement-name")]
        after["passes"][0]["nodes"][0]["target"] = ["#removed"]
        run = self.validate(bridge.migration_to_sarif(diagnose(before, after, old=old, new=new)))
        notes = {(r["properties"]["observation"], r["properties"].get("rule")): r["message"]["text"]
                 for r in run["results"]}
        removed = notes["removed", "legacy-name"]
        for text in (
            'Current original-ID page observation: rule="legacy-name", status=absent-from-current-catalog, outcomes=[].',
            'Current original-ID target observation: rule="legacy-name", target=["#removed"], status=no-observed-target-outcome, observed nodes: violations=0, passes=0.',
            'Current explicitly mapped page observation: rule="replacement-name", status=reported-in-current, outcomes=["passes","violations"].',
            'Current explicitly mapped target observation: rule="replacement-name", target=["#removed"], status=observed-target-outcome, observed nodes: violations=0, passes=1.',
        ):
            self.assertIn(text, removed)
        added = notes["added", "replacement-name"]
        self.assertIn('Baseline original-ID page observation: rule="replacement-name", status=reported-in-baseline, outcomes=["passes"].', added)
        self.assertIn('Baseline original-ID target observation: rule="replacement-name", target=["#added"], status=observed-target-outcome, observed nodes: violations=0, passes=1.', added)
        self.assertNotIn("Current original-ID", added)
        self.assertNotIn("explicitly mapped", added)
        self.assertIn('status=reported-in-current, outcomes=["inapplicable"].', notes["removed", "still-rule"])
        self.assertIn('status=not-reported-in-current, outcomes=[].', notes["removed", "skipped-rule"])
        for key in (("removed", "retired-rule"), ("removed", "still-rule"), ("removed", "skipped-rule"), ("renamed", None)):
            self.assertNotIn("explicitly mapped", notes[key])
        self.assertNotIn("page observation:", notes["renamed", None])
        self.assertNotIn("target observation:", notes["renamed", None])

    def test_messages_keep_baseline_gaps_and_shared_counts_as_observations(self):
        target = ["iframe#checkout", ["購物-shell", " button.buy "]]
        before = report(targets=[target, target, target])
        after = report("4.11.0", targets=[target, ["#added"]])
        after["passes"] = [outcome("legacy-name")]
        after["passes"][0]["nodes"][0]["target"] = target
        mapping = {**plan(), "renames": []}
        for baseline_group in (None, "inapplicable", "passes"):
            for available in (False, True):
                with self.subTest(baseline_group=baseline_group, available=available):
                    b, a = deepcopy(before), deepcopy(after)
                    a["violations"].append(outcome("other-rule"))
                    if available and baseline_group:
                        b[baseline_group] = [outcome("other-rule", baseline_group)]
                    old = catalog(rules=("legacy-name", "other-rule") if available else ("legacy-name",))
                    new = catalog("4.11.0", ("legacy-name", "other-rule"), "fixture-rules-v2")
                    run = self.validate(bridge.migration_to_sarif(diagnose(b, a, mapping, old, new)))
                    repeated = [r for r in run["results"] if r["properties"]["observation"] == "removed"]
                    self.assertEqual([r["properties"]["occurrence"] for r in repeated], [1, 2])
                    for result in repeated:
                        self.assertIn('status=observed-target-outcome, observed nodes: violations=1, passes=1.', result["message"]["text"])
                        self.assertNotIn("explicitly mapped", result["message"]["text"])
                    added = next(r for r in run["results"] if r["properties"].get("rule") == "other-rule")
                    message = added["message"]["text"]
                    status = "absent-from-baseline-catalog" if not available else "reported-in-baseline" if baseline_group else "not-reported-in-baseline"
                    self.assertIn('Baseline original-ID page observation: rule="other-rule", status=' + status, message)
                    self.assertIn('observed nodes: violations=0, passes=' + ("1." if available and baseline_group == "passes" else "0."), message)

    def validate(self, sarif):
        schema = json.loads((ROOT / "src/qawrench/a11y/schemas/migration-sarif-output.schema.json").read_text())
        Draft7Validator.check_schema(schema)
        checker = FormatChecker()
        self.assertIn("uri", checker.checkers, "Install the test extra (rfc3986-validator); URI checks must not silently skip")
        Draft7Validator(schema, format_checker=checker).validate(sarif)
        run = sarif["runs"][0]
        self.assertNotIn("gate", run["properties"])
        for result in run["results"]:
            self.assertEqual(result["level"], "note")
            self.assertIn(WARNING, result["message"]["text"])
            for field in ("baselineState", "fixes", "suppressions"):
                self.assertNotIn(field, result)
            rule = run["tool"]["driver"]["rules"][result["ruleIndex"]]
            self.assertEqual(rule["id"], result["ruleId"])
            self.assertEqual(result["ruleId"], 'a11y-migration/' + result["properties"]["observation"])
            physical = result["locations"][0]["physicalLocation"]
            self.assertEqual(set(physical), {"artifactLocation"})
            props = result["properties"]
            if props["observation"] == "renamed":
                baseline, current = props["baseline"], props["current"]
            else:
                baseline = props if props["observation"] == "removed" else None
                current = props if props["observation"] == "added" else None
            fp = result["partialFingerprints"]
            expected = {"a11yMigrationObservation/v1": hashlib.sha256(canonical([
                props["observation"], baseline["fingerprint"] if baseline else None,
                current["fingerprint"] if current else None]).encode()).hexdigest()}
            for side, finding in (("Baseline", baseline), ("Current", current)):
                if finding:
                    expected[f"a11yMigration{side}Identity/v1"] = finding["fingerprint"]
            self.assertEqual(fp, expected)
        return run

    def test_native_findings_alias_pairs_and_catalog_metadata_are_lossless(self):
        before, after, old, new = inputs()
        diagnostics = diagnose(before, after, old=old, new=new)
        original = deepcopy(diagnostics)
        sarif = bridge.migration_to_sarif(diagnostics)
        run = self.validate(sarif)
        self.assertEqual(len(run["results"]), sum(diagnostics["counts"][k] for k in ("added", "removed", "renamed")))
        for key in ("counts", "sources", "migration", "identityPolicy", "ruleCatalogs", "pages"):
            self.assertEqual(run["properties"][key], diagnostics[key])
        for status in ("added", "removed", "renamed"):
            findings = []
            for result in run["results"]:
                props = deepcopy(result["properties"])
                if props.pop("observation") == status:
                    props.pop("occurrence")
                    findings.append(props)
            self.assertEqual(sorted(findings, key=canonical), sorted(diagnostics[status], key=canonical))
        removed = next(r["properties"] for r in run["results"] if r["properties"].get("rule") == "legacy-name")
        self.assertEqual(removed["ruleCoverage"]["status"], "absent-from-current-catalog")
        self.assertEqual(removed["ruleTargetCoverage"]["rule"], "legacy-name")
        self.assertEqual(removed["mappedRuleCoverage"]["rule"], "replacement-name")
        self.assertEqual(removed["mappedTargetCoverage"]["observedNodes"], {"violations": 0, "passes": 0})
        self.assertEqual(diagnostics, original)
        removed["mappedTargetCoverage"]["target"].append("changed")
        self.assertEqual(diagnostics, original)

    def test_repeated_nested_targets_shared_counts_and_deterministic_order(self):
        nested = ["iframe#checkout", ["checkout-shell", "button.buy"]]
        flat = ["iframe#checkout", "checkout-shell", "button.buy"]
        before = report(targets=[nested, nested, nested, flat, ["#missing"]])
        after = report("4.11.0", targets=[nested])
        passed = outcome("legacy-name")
        passed["nodes"][0]["target"] = nested
        passed["nodes"] *= 2
        after["passes"] = [passed]
        mapping = {**plan(), "renames": []}
        diagnostics = diagnose(before, after, mapping, catalog(), catalog("4.11.0", ("legacy-name",), "fixture-rules-v2"))
        sarif = bridge.migration_to_sarif(diagnostics)
        run = self.validate(sarif)
        self.assertEqual(diagnostics["counts"]["unchanged"], 1)
        results = run["results"]
        repeats = [r for r in results if r["properties"]["target"] == nested]
        self.assertEqual(len(repeats), 2)
        self.assertEqual([r["properties"]["occurrence"] for r in repeats], [1, 2])
        self.assertEqual(repeats[0]["partialFingerprints"], repeats[1]["partialFingerprints"])
        for result in results:
            props = result["properties"]
            self.assertNotIn("mappedTargetCoverage", props)
            self.assertEqual(props["ruleTargetCoverage"]["observedNodes"],
                             {"violations": 1, "passes": 2} if props["target"] == nested else {"violations": 0, "passes": 0})
        replay = deepcopy(diagnostics)
        for key in ("added", "removed", "renamed", "unchanged"):
            replay[key].reverse()
        self.assertEqual(canonical(sarif), canonical(bridge.migration_to_sarif(replay)))
        self.assertEqual(canonical(sarif), canonical(bridge.migration_to_sarif(diagnostics)))

    def test_no_catalog_no_alias_and_empty_observations(self):
        mapping = {**plan(), "renames": []}
        before, after = report(), report("4.11.0", "replacement-name")
        diagnostics = diagnose(before, after, mapping)
        run = self.validate(bridge.migration_to_sarif(diagnostics))
        self.assertNotIn("ruleCatalogs", run["properties"])
        self.assertEqual({r["properties"]["observation"] for r in run["results"]}, {"added", "removed"})
        for result in run["results"]:
            self.assertNotIn("ruleCoverage", result["properties"])
        same = diagnose(before, before, {**mapping, "current": mapping["baseline"]})
        empty = self.validate(bridge.migration_to_sarif(same))
        self.assertEqual(empty["results"], [])
        self.assertEqual(empty["properties"]["counts"]["unchanged"], 2)

    def test_pa11y_namespaces_empty_selector_and_alias_fingerprints(self):
        before = json.loads((ROOT / "examples/a11y/pa11y.json").read_text())
        for issue in before["results"]["https://fixture.test/"]:
            issue.update(code="legacy-name", selector="")
        after = deepcopy(before)
        after["results"]["https://fixture.test/"][0]["code"] = "replacement-name"
        after["results"]["https://fixture.test/"][1]["code"] = "other-rule"
        baseline, current = [bridge.make_baseline(r, "pa11y-ci", input_version="4.0.0") for r in (before, after)]
        diagnostics = bridge.diagnose_migration(baseline, current, plan("pa11y-ci", "4.0.0", "4.0.0", "pa11y-ci/htmlcs"))
        run = self.validate(bridge.migration_to_sarif(diagnostics))
        for result in run["results"]:
            props = result["properties"]
            f = props["current"] if props["observation"] == "renamed" else props
            self.assertEqual(f["target"], "")
            self.assertIn(f["engine"], ("pa11y-ci/htmlcs", "pa11y-ci/axe"))
            message = result["message"]["text"]
            self.assertIn('engine=' + json.dumps(f["engine"]), message)
            self.assertIn('rule=' + json.dumps(f["rule"]), message)
            self.assertIn('target="", page="https://fixture.test/".', message)
            self.assertNotIn("page observation:", message)
            self.assertNotIn("target observation:", message)


class MigrationSarifCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        before, after, old, new = inputs()
        self.files = {key: self.path / (key + '.json') for key in ('baseline', 'report', 'map', 'old', 'new')}
        for key, value in zip(self.files, (snapshot(before), after, plan(), old, new)):
            self.files[key].write_text(json.dumps(value))
        self.command = [sys.executable, '-m', 'qawrench.a11y', 'migration', '--format', 'axe',
                        '--url', 'https://fixture.test', '--baseline', str(self.files['baseline']),
                        str(self.files['report']), '--rule-map', str(self.files['map'])]

    def cli(self, *args, expected=0):
        result = subprocess.run([*self.command, *map(str, args)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        return result

    def test_cli_sarif_format_fixture(self):
        out = self.path / 'migration.sarif'
        summary = self.cli('--output-format', 'sarif', '--out', out)
        self.assertEqual(json.loads(summary.stdout)['counts']['renamed'], 1)
        sarif = json.loads(out.read_text())
        self.assertEqual(sarif['version'], '2.1.0')
        self.assertTrue(all(r['level'] == 'note' for r in sarif['runs'][0]['results']))
        prior = out.read_bytes()
        self.cli('--output-format', 'sarif', '--out', out)
        self.assertEqual(out.read_bytes(), prior)

    def test_catalog_json_compatibility_dry_run_and_input_protection(self):
        catalogs = ['--baseline-catalog', self.files['old'], '--current-catalog', self.files['new']]
        out = self.path / 'migration.sarif'
        self.cli(*catalogs, '--output-format', 'sarif', '--out', out, '--dry-run')
        self.assertFalse(out.exists())
        self.cli(*catalogs, '--output-format', 'sarif', '--out', out)
        sarif = json.loads(out.read_text())
        self.assertIn('ruleCatalogs', sarif['runs'][0]['properties'])
        default, explicit = self.path / 'default.json', self.path / 'explicit.json'
        self.cli(*catalogs, '--out', default)
        self.cli(*catalogs, '--output-format', 'json', '--out', explicit)
        self.assertEqual(default.read_bytes(), explicit.read_bytes())
        alias = self.path / 'alias.json'
        alias.hardlink_to(self.files['map'])
        for dest in (*self.files.values(), alias):
            original = dest.read_bytes()
            self.cli(*catalogs, '--output-format', 'sarif', '--out', dest, expected=2)
            self.assertEqual(dest.read_bytes(), original)
        for forbidden in ('--exceptions', '--fail-on', '--sarif'):
            self.cli('--output-format', 'sarif', '--out', out, forbidden, 'anything', expected=2)

    def test_invalid_inputs_refused_before_creating_or_replacing_sarif(self):
        originals = {key: p.read_bytes() for key, p in self.files.items()}
        cases = [
            ('report', lambda d: d.update(incomplete=[{}])),
            ('report', lambda d: d.update(scanError={})),
            ('report', lambda d: d['testEngine'].update(version='4.99.0')),
            ('report', lambda d: d['toolOptions'].update(resultTypes=['violations'])),
            ('report', lambda d: d['inapplicable'][0].update(nodes=[{}])),
            ('report', lambda d: d.update(passes=[dict(outcome('replacement-name'), nodes=[{'target': []}])])),
            ('old', lambda d: d.update(ruleSet='wrong-provenance')),
            ('new', lambda d: d.update(rules=[])),
            ('map', lambda d: d['renames'][0].update(to='unknown-endpoint')),
            ('map', lambda d: d['renames'].append(deepcopy(d['renames'][0]))),
            ('baseline', lambda d: d.update(findings=[])),
        ]
        catalogs = ['--baseline-catalog', self.files['old'], '--current-catalog', self.files['new']]
        out = self.path / 'migration.sarif'
        for key, mutate in cases:
            with self.subTest(key=key, mutation=mutate):
                value = json.loads(originals[key]); mutate(value)
                self.files[key].write_text(json.dumps(value))
                for existing in (False, True):
                    if existing:
                        out.write_bytes(b'keep-existing-output\n')
                    elif out.exists():
                        out.unlink()
                    self.cli(*catalogs, '--output-format', 'sarif', '--out', out, expected=2)
                    if existing:
                        self.assertEqual(out.read_bytes(), b'keep-existing-output\n')
                    else:
                        self.assertFalse(out.exists())
                self.files[key].write_bytes(originals[key])
        out.unlink()
        self.cli('--baseline-catalog', self.files['old'], '--output-format', 'sarif', '--out', out, expected=2)
        self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
