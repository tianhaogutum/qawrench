"""Ordinary diff locations are URIs; retained native page identities are text."""
from collections import defaultdict
from copy import deepcopy
from datetime import date
import json
import unittest
from unittest.mock import patch

from jsonschema import Draft7Validator, FormatChecker, ValidationError

import qawrench.a11y as bridge
from qawrench.a11y.core import canonical
from qawrench.a11y.sarif import HELP
from .test_bridge import ROOT, fixture
from .test_exceptions import reviewed


def page_cases():
    return {
        'https://fixture.test/購物?b=2&a=%2f&say="yes"#區':
            'https://fixture.test/%E8%B3%BC%E7%89%A9?b=2&a=%2f&say=%22yes%22#%E5%8D%80',
        'https://fixture.test/購物?a=%2f&b=2&say="yes"#區':
            'https://fixture.test/%E8%B3%BC%E7%89%A9?a=%2f&b=2&say=%22yes%22#%E5%8D%80',
        'https://fixture.test/購物?b=2&a=%2F&say="yes"#區':
            'https://fixture.test/%E8%B3%BC%E7%89%A9?b=2&a=%2F&say=%22yes%22#%E5%8D%80',
        'https://fixture.test/購物?b=2&a=%2f&say="yes"#別':
            'https://fixture.test/%E8%B3%BC%E7%89%A9?b=2&a=%2f&say=%22yes%22#%E5%88%A5',
        'https://fixture.test/a[1]|<>^`{}?q=[x]#one#two':
            'https://fixture.test/a%5B1%5D%7C%3C%3E%5E%60%7B%7D?q=%5Bx%5D#one%23two',
        "http://[2001:db8::1]:8080/a:@!$&'()*+,;=~-._/%2f?b=2&a=%2F&x=/?:@!$'()*+,;=#/?:@!$&'()*+,;=":
            "http://[2001:db8::1]:8080/a:@!$&'()*+,;=~-._/%2f?b=2&a=%2F&x=/?:@!$'()*+,;=#/?:@!$&'()*+,;=",
        'https://fixture.test/%E8%B3%BC%E7%89%A9?b=2&a=%2f&say=%22yes%22#%E5%8D%80':
            'https://fixture.test/%E8%B3%BC%E7%89%A9?b=2&a=%2f&say=%22yes%22#%E5%8D%80',
    }


def reports(fmt, pages):
    """Every page/rule has unchanged duplicates plus new and absent targets."""
    targets = (("same", "same", "absent", "absent"),
               ("same", "same", "same", "new", "new"))
    result = []
    for side in targets:
        if fmt == "axe":
            report = fixture("axe-before.json")
            report["url"] = pages[0]
            node = report["violations"][0]["nodes"][0]
            report["violations"][0]["nodes"] = [dict(deepcopy(node), target=[" iframe#購物 ", ["checkout-shell", "#" + t]]) for t in side]
        else:
            template = fixture("pa11y.json")["results"]["https://fixture.test/"]
            report = {"total": len(pages), "passes": 0, "errors": 2 * len(side) * len(pages),
                      "results": {page: [dict(deepcopy(issue), selector="" if t == "same" else " #" + t + " ")
                                         for issue in template for t in side] for page in pages}}
        result.append(report)
    return result


def snapshots(fmt, native):
    return [bridge.make_baseline(r, fmt, **({"input_version": "4.0.0"} if fmt == "pa11y-ci" else {})) for r in native]


def help_reports(fmt, help_url):
    native = reports(fmt, [next(iter(page_cases()))])
    for report in native:
        if fmt == "axe":
            report["violations"][0]["helpUrl"] = help_url
        else:
            for issues in report["results"].values():
                for issue in issues:
                    if issue["runner"] == "axe":
                        if help_url is None:
                            issue["runnerExtras"].pop("helpUrl", None)
                        else:
                            issue["runnerExtras"]["helpUrl"] = help_url
    return native


class DiffSarifUriTests(unittest.TestCase):
    def setUp(self):
        checker = FormatChecker()
        self.assertIn("uri", checker.checkers, "Install the test extra (rfc3986-validator); URI validation must run")
        self.uri = Draft7Validator({"type": "string", "format": "uri"}, format_checker=checker)
        self.schema = json.loads((ROOT / "src/qawrench/a11y/schemas/sarif-output.schema.json").read_text())
        Draft7Validator.check_schema(self.schema)
        self.validator = Draft7Validator(self.schema, format_checker=checker)

    def test_strict_checker_rejects_invalid_uri_text(self):
        for value in ('https://fixture.test/購物', 'https://fixture.test/?x="y"',
                      'https://fixture.test/%xx', '/relative', 'https://fixture.test/#one#two',
                      'https://fixture.test/a[1]'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.uri.validate(value)
        for uri in page_cases().values():
            self.uri.validate(uri)

    def test_locations_preserve_native_states_duplicates_reviews_and_gates(self):
        cases = page_cases()
        for fmt in ("axe", "pa11y-ci"):
            page_sets = [[page] for page in cases] if fmt == "axe" else [list(cases)]
            for pages in page_sets:
                before, after = snapshots(fmt, reports(fmt, pages))
                for budget in (None, 2, 3):
                    with self.subTest(fmt=fmt, pages=pages, budget=budget):
                        review = None
                        if budget is not None:
                            review = reviewed(after["findings"][0])
                            identities = {f["fingerprint"]: f for f in after["findings"]}
                            review["exceptions"] = [reviewed(f, maxOccurrences=budget)["exceptions"][0] for f in identities.values()]
                        delta = bridge.compare(before, after, fail_on="minor", exceptions=review, evaluation_date=date(2026, 11, 11))
                        original = deepcopy(delta)
                        with patch("qawrench.a11y.core.normalize_url", side_effect=AssertionError("re-normalized page")):
                            sarif = bridge.to_sarif(delta)
                        run = sarif["runs"][0]
                        groups = defaultdict(list)
                        native = {f["fingerprint"]: f for f in before["findings"] + after["findings"]}
                        self.assertEqual({r["baselineState"] for r in run["results"]}, {"new", "absent", "unchanged"})
                        for r in run["results"]:
                            props = r["properties"]
                            page = props["pageUrl"]
                            self.assertEqual(r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], cases[page])
                            f = native[r["partialFingerprints"]["a11yBaselineIdentity/v1"]]
                            self.assertEqual((props["native"], props["selector"], r["message"]["text"]), (f["native"], f["target"], f["message"]))
                            self.assertEqual(r["level"], {"critical": "error", "serious": "error", "moderate": "warning"}[props["severity"]])
                            groups[f["fingerprint"]].append(r)
                        for occurrences in groups.values():
                            self.assertEqual([r["properties"]["occurrence"] for r in occurrences], list(range(1, len(occurrences) + 1)))
                        self.assertEqual(len({f["page"] for f in native.values()}), len(pages))
                        self.assertEqual(delta["gate"]["passed"], budget == 3)
                        self.assertEqual(run["properties"], {key: delta[key] for key in ("counts", "gate", "identityPolicy", *(("exceptions",) if review else ()))})
                        self.assertEqual(delta, original)
                        self.validator.validate(sarif)
                        replay = deepcopy(delta)
                        for key in ("new", "fixed", "unchanged"):
                            replay[key].reverse()
                        self.assertEqual(canonical(sarif), canonical(bridge.to_sarif(replay)))

    def test_schema_separates_native_page_text_from_artifact_uri(self):
        before, after = snapshots("axe", reports("axe", [next(iter(page_cases()))]))
        sarif = bridge.to_sarif(bridge.compare(before, after))
        # Isolate the schema boundary from the encoder, including on the prior implementation.
        for r in sarif["runs"][0]["results"]:
            r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = page_cases()[r["properties"]["pageUrl"]]
        self.validator.validate(sarif)
        for value in (23, "/relative", "ftp://fixture.test/file"):
            bad = deepcopy(sarif)
            bad["runs"][0]["results"][0]["properties"]["pageUrl"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.validator.validate(bad)
        bad = deepcopy(sarif)
        bad["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = next(iter(page_cases()))
        errors = list(self.validator.iter_errors(bad))
        self.assertEqual(len(errors), 1)
        self.assertEqual(list(errors[0].absolute_path)[-1], "uri")

    def test_native_help_links_pass_strict_schema_without_rewriting_findings(self):
        for fmt in ("axe", "pa11y-ci"):
            for help_url, expected in page_cases().items():
                for budget in (2, 3):
                    with self.subTest(fmt=fmt, help_url=help_url, budget=budget):
                        before, after = snapshots(fmt, help_reports(fmt, help_url))
                        identities = {f["fingerprint"]: f for f in after["findings"]}
                        review = reviewed(after["findings"][0])
                        review["exceptions"] = [reviewed(f, maxOccurrences=budget)["exceptions"][0]
                                                for f in identities.values()]
                        delta = bridge.compare(before, after, fail_on="minor", exceptions=review,
                                               evaluation_date=date(2026, 11, 11))
                        original = deepcopy(delta)
                        with patch("qawrench.a11y.core.normalize_url", side_effect=AssertionError("re-normalized URL")):
                            sarif = bridge.to_sarif(delta)
                        # This validation fails specifically at rule.helpUri on the old formatter.
                        self.validator.validate(sarif)
                        run = sarif["runs"][0]
                        for rule in run["tool"]["driver"]["rules"]:
                            engine = rule["properties"]["engine"]
                            self.assertEqual(rule["helpUri"], HELP[engine] if engine.endswith("htmlcs") else expected)
                            self.uri.validate(rule["helpUri"])
                        for result in run["results"]:
                            self.uri.validate(result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"])
                            props = result["properties"]
                            if props["engine"].endswith("htmlcs"):
                                continue
                            native_url = (props["native"]["rule"]["helpUrl"] if fmt == "axe"
                                          else props["native"]["runnerExtras"]["helpUrl"])
                            self.assertEqual(native_url, help_url)
                        self.assertEqual(delta, original)
                        self.assertEqual(delta["gate"]["passed"], budget == 3)
                        self.assertTrue(all(f["helpUrl"] == help_url for f in after["findings"]
                                            if not f["engine"].endswith("htmlcs")))
                        bad = deepcopy(sarif)
                        bad["runs"][0]["tool"]["driver"]["rules"][0]["helpUri"] = 'https://fixture.test/購物#one#two'
                        errors = list(self.validator.iter_errors(bad))
                        self.assertEqual(len(errors), 1)
                        self.assertEqual(list(errors[0].absolute_path)[-1], "helpUri")

    def test_help_fallbacks_and_repeated_rule_selection_remain_deterministic(self):
        for url in HELP.values():
            self.uri.validate(url)
        before, after = snapshots("pa11y-ci", help_reports("pa11y-ci", None))
        fallback = bridge.to_sarif(bridge.compare(before, after))
        self.assertEqual({r["properties"]["engine"]: r["helpUri"] for r in fallback["runs"][0]["tool"]["driver"]["rules"]},
                         {engine: url for engine, url in HELP.items() if engine != "axe"})
        for fmt in ("axe", "pa11y-ci"):
            native = help_reports(fmt, next(iter(page_cases())))
            for report in native:
                if fmt == "axe":
                    repeated = deepcopy(report["violations"][0])
                    repeated["helpUrl"] = list(page_cases())[2]
                    report["violations"].append(repeated)
                else:
                    for issues in report["results"].values():
                        repeated = deepcopy([i for i in issues if i["runner"] == "axe"])
                        for issue in repeated:
                            issue["runnerExtras"]["helpUrl"] = list(page_cases())[2]
                        issues.extend(repeated)
                        report["errors"] += len(repeated)
            before, after = snapshots(fmt, native)
            delta = bridge.compare(before, after)
            expected = {}
            occurrences = [("new", f) for f in delta["new"]] + [("absent", f) for f in delta["fixed"]]
            occurrences += [("unchanged", p["current"]) for p in delta["unchanged"]]
            occurrences.sort(key=lambda item: (item[1]["engine"], item[1]["rule"], item[1]["fingerprint"], item[0], canonical(item[1])))
            for _, finding in occurrences:
                expected.setdefault(f'{finding["engine"]}:{finding["rule"]}',
                                    page_cases().get(finding["helpUrl"], HELP[finding["engine"]]))
            sarif = bridge.to_sarif(delta)
            self.assertEqual({r["id"]: r["helpUri"] for r in sarif["runs"][0]["tool"]["driver"]["rules"]}, expected)
            for key in ("new", "fixed", "unchanged"):
                delta[key].reverse()
            self.assertEqual(sarif, bridge.to_sarif(delta))

    def test_native_help_url_validation_remains_strict(self):
        invalid = (None, "", "/relative", "ftp://fixture.test/a", "https://user:pw@fixture.test/",
                   "https://fixture.test/%xx", "https://fixture.test/space here", "https://fixture.test/\n",
                   "https://fixture.test/\\x", "https://fixture.test:bad/", "https://[bad]/")
        for fmt in ("axe", "pa11y-ci"):
            for value in invalid:
                native = help_reports(fmt, "https://fixture.test/help")
                if fmt == "axe":
                    native[0]["violations"][0]["helpUrl"] = value
                else:
                    for issues in native[0]["results"].values():
                        next(i for i in issues if i["runner"] == "axe")["runnerExtras"]["helpUrl"] = value
                with self.subTest(fmt=fmt, value=value), self.assertRaises(bridge.BridgeError):
                    snapshots(fmt, native)
